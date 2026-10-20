import base64
import hashlib
import hmac
import re
import secrets
import time
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fastapi import HTTPException
from .config import now
from .db import settings, set_setting


def fail(message, status=400):
    raise HTTPException(status, message)


def text_value(value, label, limit=200, required=True):
    if not isinstance(value, str):
        fail(f'{label}格式不正确')
    value = value.strip()
    if (required and not value) or len(value) > limit:
        fail(f'{label}不能为空且最多 {limit} 个字符' if required else f'{label}最多 {limit} 个字符')
    return value


def hash_password(password):
    if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
        fail('密码长度需为 10–128 个字符')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
    return f'{salt}${digest}'


def verify_password(password, encoded):
    if not isinstance(password, str) or len(password) > 128:
        return False
    salt, expected = encoded.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
    return hmac.compare_digest(actual, expected)


def admin(user):
    if not user['active']:
        fail('账号已停用', 403)


def owner(user):
    admin(user)


def public_user(user):
    return {k: user[k] for k in ('id', 'username', 'name', 'role', 'active')}


def currency(conf, code):
    match = next((c for c in conf['currencies'] if c['code'] == code), None)
    if not match:
        fail('请选择团队已启用的币种')
    return match


def minor_amount(value, decimals):
    value = str(value).strip()
    if not re.fullmatch(r'\d{1,12}(?:\.\d{1,4})?', value):
        fail('金额必须为正数，不支持千分位或科学计数法')
    try:
        amount = Decimal(value)
        scaled = amount * (10 ** decimals)
        if amount <= 0 or scaled != scaled.to_integral_value() or scaled > 9_000_000_000_000:
            fail(f'金额超出范围或小数超过 {decimals} 位')
        return int(scaled)
    except InvalidOperation:
        fail('金额格式不正确')


def normalized_entry(conn, user, body):
    conf = settings(conn)
    kind = body.get('kind', 'expense')
    if kind not in ('expense', 'income'):
        fail('账目类型不正确')
    if kind == 'income':
        admin(user)
    cur = currency(conf, body.get('currency', conf['default_currency']))
    amount = minor_amount(body.get('amount', ''), cur['decimals'])
    description = text_value(body.get('description', ''), '用途 / 收入说明', 120, False)
    try:
        entry_date = date.fromisoformat(body.get('date', ''))
        if entry_date.year < 2000 or entry_date.year > 2100:
            raise ValueError()
    except (ValueError, TypeError):
        fail('日期格式不正确')
    payer_id = body.get('payer_id', user['id'])
    if not isinstance(payer_id, int):
        fail('成员格式不正确')
    if payer_id != user['id']:
        admin(user)
    if not conn.execute('SELECT id FROM users WHERE id=? AND active=1', (payer_id,)).fetchone():
        fail('成员不存在或已停用')
    status = body.get('status', 'pending') if kind == 'expense' else body.get('status', 'posted')
    if kind == 'income' and status == 'pending':
        status = 'posted'  # Existing forms used pending for income.
    if status == 'submitted':
        status = 'pending'  # Older open forms and persisted Telegram drafts remain usable.
    if status not in (('draft', 'pending') if kind == 'expense' else ('posted', 'distributed')):
        fail('支出请选择草稿或待报销，收入请选择团队在账或已分红')
    return {'kind': kind, 'amount_minor': amount, 'currency': cur['code'], 'decimals': cur['decimals'],
            'description': description, 'category': '', 'date': entry_date.isoformat(),
            'payer_id': payer_id, 'status': status, 'note': text_value(body.get('note', ''), '备注', 1000, False)}


def entry_view(conn, entry_id):
    row = conn.execute('SELECT e.*,u.name payer_name,c.name creator_name FROM entries e JOIN users u ON u.id=e.payer_id JOIN users c ON c.id=e.created_by WHERE e.id=?', (entry_id,)).fetchone()
    if not row:
        fail('账目不存在', 404)
    result = dict(row)
    def amount(units):
        return format(Decimal(units) / (10 ** row['decimals']), f'.{row["decimals"]}f')
    result['amount'] = amount(row['amount_minor'])
    result['reimbursed_amount'] = amount(row['reimbursed_minor'])
    result['remaining_minor'] = row['amount_minor'] - row['reimbursed_minor'] if row['kind'] == 'expense' else 0
    result['remaining_amount'] = amount(result['remaining_minor'])
    result['version'] = conn.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE entry_id=?', (entry_id,)).fetchone()[0]
    result['payments'] = []
    for payment in conn.execute('SELECT p.*,u.name FROM payments p JOIN users u ON u.id=p.user_id WHERE entry_id=? ORDER BY p.id', (entry_id,)):
        item = {k: payment[k] for k in ('id', 'user_id', 'name', 'currency', 'decimals', 'amount_minor', 'entry_amount_minor', 'fx_rate', 'fx_date', 'note', 'created_at')}
        item['amount'] = format(Decimal(payment['amount_minor']) / (10 ** payment['decimals']), f'.{payment["decimals"]}f')
        item['entry_amount'] = amount(payment['entry_amount_minor'])
        result['payments'].append(item)
    result['receipt'] = bool(result['receipt'])
    result.pop('category', None)  # Retained database column only for compatibility with earlier local builds.
    return result


def audit(conn, entry_id, user_id, action, detail=''):
    conn.execute('INSERT INTO events(entry_id,user_id,action,detail,created_at) VALUES (?,?,?,?,?)',
                 (entry_id, user_id, action, detail, now()))


def add_entry(conn, user, body, source='web', source_key=None):
    data = normalized_entry(conn, user, body)
    if not source_key or len(source_key) > 200:
        fail('缺少有效的防重复提交标识')
    existing = conn.execute('SELECT id,created_by FROM entries WHERE source_key=?', (source_key,)).fetchone()
    if existing:
        if existing['created_by'] != user['id']:
            fail('重复提交标识冲突', 409)
        previous = entry_view(conn, existing['id'])
        if any(previous[k] != v for k, v in data.items() if k not in ('status', 'category')):
            fail('同一提交标识不能用于不同账目', 409)
        return previous
    timestamp = now()
    cols = list(data) + ['created_by', 'source', 'source_key', 'created_at', 'updated_at']
    values = list(data.values()) + [user['id'], source, source_key, timestamp, timestamp]
    cursor = conn.execute(f'INSERT INTO entries({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})', values)
    audit(conn, cursor.lastrowid, user['id'], 'created', f'通过 {source} 创建')
    return entry_view(conn, cursor.lastrowid)


def make_payment_quote(conn, user, entry_id, code, fx=None):
    admin(user)
    entry, conf = entry_view(conn, entry_id), settings(conn)
    if entry['kind'] != 'expense' or entry['status'] != 'pending':
        fail('只有待报销账目可以登记付款', 409)
    code = entry['currency'] if code == 'original' else code
    if code not in (entry['currency'], conf['reference_currency']):
        fail('请选择记账货币或团队结算货币')
    cur = currency(conf, code)
    rate, rate_date = Decimal(1), None
    if code != entry['currency']:
        row = (fx or {}).get('rates', {}).get(entry['currency'])
        if not row or fx.get('base') != code or fx.get('stale') or fx.get('missing'):
            fail('暂无有效汇率，不能按结算货币登记，请刷新汇率或选择记账货币')
        rate, rate_date = Decimal(row['rate']), row['date']
    maximum = int((Decimal(entry['remaining_minor']) / (10 ** entry['decimals']) * rate * (10 ** cur['decimals'])).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if maximum <= 0:
        fail('剩余金额小于该币种最小单位，请按记账货币报销')
    token, expires = secrets.token_urlsafe(24), int(time.time()) + 1800
    conn.execute('DELETE FROM payment_quotes WHERE expires<?', (int(time.time()) - 86400,))
    conn.execute('INSERT INTO payment_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (token, entry_id, user['id'], entry['version'], code, cur['decimals'], str(rate), rate_date, entry['remaining_minor'], maximum, expires))
    return {'id': token, 'entry_id': entry_id, 'version': entry['version'], 'currency': code, 'decimals': cur['decimals'],
            'rate': str(rate), 'rate_date': rate_date, 'remaining_amount': entry['remaining_amount'], 'entry_currency': entry['currency'],
            'maximum_amount': format(Decimal(maximum) / (10 ** cur['decimals']), f'.{cur["decimals"]}f'), 'expires': expires}


def payment_preview(conn, user, entry_id, amount, quote_id):
    entry = entry_view(conn, entry_id)
    quote = conn.execute('SELECT * FROM payment_quotes WHERE id=? AND entry_id=? AND user_id=?', (quote_id, entry_id, user['id'])).fetchone()
    if not quote or quote['expires'] <= time.time():
        fail('付款换算已过期，请重新选择付款币种', 409)
    if entry['status'] != 'pending' or entry['version'] != quote['version']:
        fail('账目或已报销金额已变化，请刷新后核对再登记', 409)
    units = minor_amount(amount, quote['decimals'])
    if units > quote['maximum_minor']:
        fail('本次报销金额不能超过所选币种的剩余待报销金额')
    original_units = entry['remaining_minor'] if units == quote['maximum_minor'] else int((Decimal(units) / (10 ** quote['decimals']) / Decimal(quote['rate']) * (10 ** entry['decimals'])).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if not 0 < original_units <= entry['remaining_minor']:
        fail('折算金额小于记账货币最小单位，请调整金额或选择记账货币')
    if original_units == entry['remaining_minor'] and units != quote['maximum_minor']:
        fail('金额接近结清，请填写剩余全额或调低本次报销金额')
    return entry, dict(quote), units, original_units


def record_payment(conn, user, entry_id, amount, note, payment_key, quote_id):
    admin(user)
    note = text_value(note, '付款方式 / 付款参考号', 500)
    if not isinstance(payment_key, str) or not 16 <= len(payment_key) <= 200:
        fail('缺少有效的付款防重复标识，请刷新后重试')
    previous = conn.execute('SELECT * FROM payments WHERE source_key=?', (payment_key,)).fetchone()
    if previous:
        units = minor_amount(amount, previous['decimals'])
        if (previous['entry_id'], previous['user_id'], previous['amount_minor'], previous['note'], previous['quote_id']) != (entry_id, user['id'], units, note, quote_id):
            fail('同一付款标识不能用于不同的付款内容', 409)
        return entry_view(conn, entry_id)
    entry, quote, units, original_units = payment_preview(conn, user, entry_id, amount, quote_id)
    paid, timestamp = entry['reimbursed_minor'] + original_units, now()
    conn.execute('INSERT INTO payments(entry_id,user_id,amount_minor,currency,decimals,note,entry_amount_minor,fx_rate,fx_date,quote_id,source_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (entry_id, user['id'], units, quote['currency'], quote['decimals'], note, original_units, quote['rate'], quote['rate_date'], quote_id, payment_key, timestamp))
    conn.execute('UPDATE entries SET reimbursed_minor=?,status=?,review_note=?,paid_at=?,updated_at=? WHERE id=?',
                 (paid, 'reimbursed' if paid == entry['amount_minor'] else 'pending', note, timestamp, timestamp, entry_id))
    actual = format(Decimal(units) / (10 ** quote['decimals']), f'.{quote["decimals"]}f')
    allocation = format(Decimal(original_units) / (10 ** entry['decimals']), f'.{entry["decimals"]}f')
    detail = f'本次报销 {actual} {quote["currency"]}'
    if quote['currency'] != entry['currency']:
        detail += f'，抵扣 {allocation} {entry["currency"]}（1 {entry["currency"]} = {quote["rate"]} {quote["currency"]} · {quote["rate_date"]}）'
    audit(conn, entry_id, user['id'], 'pay', detail + ' · ' + note)
    return entry_view(conn, entry_id)


def transition(conn, user, entry_id, action, note='', *, amount=None, payment_key=None, quote_id=None):
    entry = entry_view(conn, entry_id)
    if action == 'void':
        if entry['kind'] == 'income' or entry['payer_id'] != user['id']:
            admin(user)
        if entry['reimbursed_minor'] or entry['status'] in ('reimbursed', 'voided'):
            fail('已有报销记录或已作废的账目不能作废', 409)
        note = text_value(note, '作废原因', 500)
        conn.execute('UPDATE entries SET status=?,review_note=?,updated_at=? WHERE id=?', ('voided', note, now(), entry_id))
        audit(conn, entry_id, user['id'], 'void', note)
        return entry_view(conn, entry_id)
    if action == 'distribute':
        admin(user)
        if entry['kind'] != 'income' or entry['status'] != 'posted':
            fail('只有团队在账收入可以标记已分红，请刷新后核对', 409)
        note = text_value(note, '分红说明', 500)
        conn.execute('UPDATE entries SET status=?,review_note=?,updated_at=? WHERE id=?', ('distributed', note, now(), entry_id))
        audit(conn, entry_id, user['id'], 'distribute', note)
        return entry_view(conn, entry_id)
    if entry['kind'] != 'expense':
        fail('收入无需报销')
    if action == 'pay':
        return record_payment(conn, user, entry_id, amount, note, payment_key, quote_id)
    note = text_value(note, '处理备注', 500, False)
    transitions = {'submit': (('draft',), 'pending')}
    if action not in transitions:
        fail('操作不正确')
    if action == 'submit':
        if entry['payer_id'] != user['id']:
            admin(user)
    else:
        admin(user)
    allowed, target = transitions[action]
    if entry['status'] not in allowed:
        fail('账目状态已变化，请刷新后重试', 409)
    conn.execute('UPDATE entries SET status=?,review_note=?,paid_at=?,updated_at=? WHERE id=?',
                 (target, note, None, now(), entry_id))
    audit(conn, entry_id, user['id'], action, note)
    return entry_view(conn, entry_id)


def delete_entry(conn, user, entry_id):
    admin(user)
    row = conn.execute('SELECT id,status,receipt FROM entries WHERE id=?', (entry_id,)).fetchone()
    if not row:
        fail('账目不存在', 404)
    if row['status'] != 'voided':
        fail('仅已作废账目可以永久删除', 409)
    # Remove all related rows; callers delete the receipt file after commit.
    conn.execute('DELETE FROM payment_quotes WHERE entry_id=?', (entry_id,))
    conn.execute('DELETE FROM payments WHERE entry_id=?', (entry_id,))
    conn.execute('DELETE FROM events WHERE entry_id=?', (entry_id,))
    conn.execute('DELETE FROM entries WHERE id=?', (entry_id,))
    return row['receipt']


def edit_entry(conn, user, entry_id, body):
    old = entry_view(conn, entry_id)
    if old['kind'] != 'expense' or old['status'] not in ('draft', 'pending') or old['reimbursed_minor']:
        fail('仅没有报销记录的草稿或待报销支出可以修改', 409)
    if old['payer_id'] != user['id']:
        admin(user)
    data = normalized_entry(conn, user, body)
    if data['kind'] != old['kind']:
        fail('不支持更改账目类型')
    conn.execute('UPDATE entries SET ' + ','.join(f'{k}=?' for k in data) + ',review_note=?,updated_at=? WHERE id=?',
                 list(data.values()) + ['', now(), entry_id])
    audit(conn, entry_id, user['id'], 'edited')
    return entry_view(conn, entry_id)


def save_settings(conn, user, body):
    owner(user)
    conf = settings(conn)
    name = text_value(body.get('team_name', conf['team_name']), '团队名称', 40)
    currencies = body.get('currencies', conf['currencies'])
    if not isinstance(currencies, list) or not 1 <= len(currencies) <= 30:
        fail('请配置 1–30 个币种')
    codes = set()
    clean = []
    for cur in currencies:
        if not isinstance(cur, dict) or not re.fullmatch('[A-Z]{3}', str(cur.get('code', ''))):
            fail('币种代码必须为 3 位大写英文字母')
        code = cur['code']
        decimals = cur.get('decimals', 2)
        if type(decimals) is not int or decimals not in (0, 2, 3, 4) or code in codes:
            fail('币种不能重复，小数位支持 0、2、3、4')
        used = conn.execute('SELECT decimals FROM entries WHERE currency=? UNION ALL SELECT decimals FROM payments WHERE currency=? LIMIT 1', (code, code)).fetchone()
        if used and used['decimals'] != decimals:
            fail(f'{code} 已有账目，不能修改其小数位')
        codes.add(code)
        clean.append({'code': code, 'decimals': decimals, 'name': text_value(cur.get('name', code), '币种名称', 20),
                      'symbol': text_value(cur.get('symbol', code), '货币符号', 8)})
    for row in conn.execute('SELECT currency FROM entries UNION SELECT currency FROM payments'):
        if row['currency'] not in codes:
            fail(f'{row["currency"]} 已有账目，不能移除')
    default = body.get('default_currency', conf['default_currency'])
    reference = body.get('reference_currency', conf['reference_currency'])
    if default not in codes or reference not in codes:
        fail('默认币种和汇总币种必须在启用币种中')
    chats = body.get('telegram_allowed_chats', conf['telegram_allowed_chats'])
    if not isinstance(chats, list) or len(chats) > 30 or any(not re.fullmatch(r'-?\d{1,20}', str(c)) for c in chats):
        fail('Telegram 群组 ID 格式不正确')
    values = dict(team_name=name, currencies=clean, default_currency=default, reference_currency=reference,
                  telegram_allowed_chats=list(dict.fromkeys(str(c) for c in chats)))
    for key, value in values.items():
        set_setting(conn, key, value)
    return settings(conn)
