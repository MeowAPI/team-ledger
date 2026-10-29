"""Record dividends within real available cash, even with reimbursements due."""
import hashlib
import json
import secrets
import time
from decimal import Decimal, ROUND_DOWN
from .config import now
from .db import settings
from .domain import admin, currency, fail, minor_amount, text_value


def rows(conn):
    entries = [dict(e) for e in conn.execute('SELECT * FROM entries ORDER BY id')]
    payments = [dict(p) for p in conn.execute('SELECT * FROM payments ORDER BY id')]
    dividends = [dict(d) for d in conn.execute('SELECT * FROM dividends ORDER BY id')]
    return entries, payments, dividends


def fingerprint(conn):
    conf = settings(conn)
    event_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
    data = [rows(conn), event_id, conf['reference_currency'], conf['currencies']]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def needed_codes(conn):
    return {r[0] for r in conn.execute("SELECT currency FROM entries WHERE kind='income' AND status='posted' UNION SELECT currency FROM payments UNION SELECT currency FROM dividends")}


def quote(conn, user, code, rates):
    admin(user)
    conf = settings(conn)
    cur = currency(conf, code)
    base = conf['reference_currency']
    if not rates or rates['base'] != base or rates.get('stale') or rates.get('missing') or (needed_codes(conn) | {code}) - rates['rates'].keys():
        fail('暂无完整有效汇率，请刷新后再登记分红。')
    entries, payments, dividends = rows(conn)

    def value(row, units=None):
        return Decimal(row['amount_minor'] if units is None else units) / (10 ** row['decimals']) * Decimal(rates['rates'][row['currency']]['rate'])

    income = sum((value(e) for e in entries if e['kind'] == 'income' and e['status'] == 'posted'), Decimal(0))
    paid = sum((value(p) for p in payments), Decimal(0))
    distributed = sum((value(d) for d in dividends), Decimal(0))
    surplus = max(income - paid - distributed, Decimal(0))
    rate = rates['rates'][code]
    maximum = int((surplus / Decimal(rate['rate']) * (10 ** cur['decimals'])).to_integral_value(rounding=ROUND_DOWN))
    token, expires = secrets.token_urlsafe(24), int(time.time()) + 1800
    conn.execute('DELETE FROM dividend_quotes WHERE expires<?', (int(time.time()) - 86400,))
    conn.execute('INSERT INTO dividend_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (token, user['id'], fingerprint(conn), code, cur['decimals'], rate['rate'], rate.get('date'), base, maximum, str(surplus), json.dumps(rates), expires))
    # All amounts here derive from the actual ledger; simulated income is never accepted.
    return view_quote(conn, user, token)


def view_quote(conn, user, quote_id):
    if not isinstance(quote_id, str) or len(quote_id) > 100:
        fail('分红确认格式不正确')
    row = conn.execute('SELECT * FROM dividend_quotes WHERE id=? AND user_id=?', (quote_id, user['id'])).fetchone()
    if not row:
        fail('分红确认不存在或不属于当前成员', 409)
    result = {k: row[k] for k in ('id', 'currency', 'decimals', 'rate', 'rate_date', 'reference_currency', 'surplus', 'expires')}
    result['maximum_amount'] = format(Decimal(row['maximum_minor']) / (10 ** row['decimals']), f'.{row["decimals"]}f')
    return result


def preview(conn, user, quote_id, amount):
    view_quote(conn, user, quote_id)
    quoted = conn.execute('SELECT * FROM dividend_quotes WHERE id=? AND user_id=?', (quote_id, user['id'])).fetchone()
    if not quoted or quoted['expires'] <= time.time():
        fail('分红确认已过期，请重新计算盈余', 409)
    if quoted['fingerprint'] != fingerprint(conn):
        fail('账本或报销金额已变化，请重新核对可分红盈余，尚未登记分红', 409)
    if quoted['maximum_minor'] <= 0:
        fail('当前没有可用于分红的实际可用资金')
    units = quoted['maximum_minor'] if amount == 'all' else minor_amount(amount, quoted['decimals'])
    if units > quoted['maximum_minor']:
        fail('本次分红不能超过扣除已报销和历史分红后的实际可用资金')
    actual = Decimal(units) / (10 ** quoted['decimals'])
    return {**view_quote(conn, user, quote_id), 'amount': format(actual, f'.{quoted["decimals"]}f'),
            'amount_minor': units, 'remaining_surplus': str(Decimal(quoted['surplus']) - actual * Decimal(quoted['rate']))}


def history(conn):
    result = []
    for row in conn.execute('SELECT d.*,u.name FROM dividends d JOIN users u ON u.id=d.user_id ORDER BY d.id DESC'):
        data = dict(row)
        data['amount'] = format(Decimal(row['amount_minor']) / (10 ** row['decimals']), f'.{row["decimals"]}f')
        data.pop('source_key')
        result.append(data)
    return result


def record(conn, user, quote_id, amount, note, key):
    admin(user)
    if not isinstance(quote_id, str) or len(quote_id) > 100:
        fail('分红确认格式不正确')
    note = text_value(note, '分红说明 / 转账参考号', 500)
    if not isinstance(key, str) or not 16 <= len(key) <= 200:
        fail('缺少分红防重复标识')
    previous = conn.execute('SELECT * FROM dividends WHERE source_key=?', (key,)).fetchone()
    if previous:
        units = previous['amount_minor'] if amount == 'all' else minor_amount(amount, previous['decimals'])
        if (previous['user_id'], previous['quote_id'], previous['amount_minor'], previous['note']) != (user['id'], quote_id, units, note):
            fail('同一分红标识不能用于不同内容', 409)
        return next(d for d in history(conn) if d['id'] == previous['id'])
    if conn.execute('SELECT 1 FROM dividends WHERE quote_id=?', (quote_id,)).fetchone():
        fail('本次分红已登记，请刷新后查看分红记录', 409)
    planned = preview(conn, user, quote_id, amount)
    cursor = conn.execute('INSERT INTO dividends(user_id,amount_minor,currency,decimals,note,reference_currency,fx_rate,fx_date,quote_id,source_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                          (user['id'], planned['amount_minor'], planned['currency'], planned['decimals'], note, planned['reference_currency'], planned['rate'], planned['rate_date'], quote_id, key, now()))
    return next(d for d in history(conn) if d['id'] == cursor.lastrowid)
