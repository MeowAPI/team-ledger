"""Shared funds report and reimbursement planning (Telegram worker + AI assistant)."""
import hashlib
import json
import time
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING, ROUND_HALF_UP
from . import fx
from .db import settings
from .domain import currency, fail, make_payment_quote, minor_amount, payment_preview

DEFAULT_NOTE = '按资金建议批量登记报销'


def ledger_rows(conn):
    entries = [dict(e) for e in conn.execute('SELECT e.*,u.name payer_name FROM entries e JOIN users u ON u.id=e.payer_id ORDER BY e.id')]
    payments = [dict(p) for p in conn.execute('SELECT * FROM payments ORDER BY id')]
    return entries, payments


def snapshot(conn):
    conf = settings(conn)
    entries, payments = ledger_rows(conn)
    return hashlib.sha256(json.dumps([entries, payments, conf['reference_currency'], conf['currencies']], sort_keys=True).encode()).hexdigest()


def report(conn, rates, scenario=None):
    conf = settings(conn)
    if not rates or rates['base'] != conf['reference_currency']:
        return None
    entries, payments = ledger_rows(conn)
    needed = {e['currency'] for e in entries if e['status'] in ('pending', 'reimbursed', 'posted')}
    needed.update(p['currency'] for p in payments)
    if scenario:
        needed.add(scenario['currency'])
    if needed - rates['rates'].keys():
        return None
    return fx.calculate(entries, conf, rates, scenario, payments)


def build_plan(conn, user, rates, mode, *, payer_id=None, entry_ids=None, date_from=None, date_to=None, note=DEFAULT_NOTE):
    """Build a cash-limited reimbursement plan and reserve payment quotes for it.

    Optional filters narrow which pending expenses are considered: a single member,
    an explicit id set, or an occurrence-date range. The cash limit still applies.
    """
    conf = settings(conn)
    result = report(conn, rates)
    if not result or not result['complete'] or rates.get('stale'):
        fail('暂无完整有效汇率，请刷新资金计算后重试。')
    base = conf['reference_currency']
    scale = 10 ** currency(conf, base)['decimals']
    entries, payments = ledger_rows(conn)
    def converted(e):
        return Decimal(e['amount_minor']) / (10 ** e['decimals']) * Decimal(rates['rates'][e['currency']]['rate'])
    cash = sum((converted(e) for e in entries if e['kind'] == 'income' and e['status'] == 'posted'), Decimal(0)) - sum((converted(p) for p in payments), Decimal(0))
    left = max(cash, Decimal(0)).quantize(Decimal(1) / scale, rounding=ROUND_DOWN)
    want_ids = {int(i) for i in entry_ids} if entry_ids else None
    items, totals, cost = [], {}, Decimal(0)
    pending = sorted((e for e in entries if e['status'] == 'pending'), key=lambda e: (e['date'], e['id']))
    for e in pending:
        if left <= 0:
            break
        if want_ids is not None and e['id'] not in want_ids:
            continue
        if payer_id is not None and e['payer_id'] != payer_id:
            continue
        if date_from and e['date'] < date_from:
            continue
        if date_to and e['date'] > date_to:
            continue
        remaining = e['amount_minor'] - e['reimbursed_minor']
        rate = Decimal(rates['rates'][e['currency']]['rate'])
        code = base if mode == 'settlement' else e['currency']
        quote = make_payment_quote(conn, user, e['id'], code, rates)
        pay_scale = 10 ** quote['decimals']
        max_units = minor_amount(quote['maximum_amount'], quote['decimals'])
        units = min(max_units, int((left * pay_scale / (rate if mode == 'original' else 1)).to_integral_value(rounding=ROUND_DOWN)))
        # A rounded partial installment must not accidentally settle the entire
        # original remainder. Match payment_preview's HALF_UP allocation rule.
        if code != e['currency'] and units < max_units:
            safe = int(((Decimal(remaining) - Decimal('0.5')) / (10 ** e['decimals']) * Decimal(quote['rate']) * pay_scale).to_integral_value(rounding=ROUND_CEILING)) - 1
            units = min(units, safe)
        if units <= 0:
            break
        amount = format(Decimal(units) / pay_scale, f'.{quote["decimals"]}f')
        # Tiny converted payments may not credit one original minor unit.
        credited = remaining if units == max_units else int((Decimal(units) / pay_scale / Decimal(quote['rate']) * (10 ** e['decimals'])).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if credited <= 0:
            break
        entry, _, _, credited = payment_preview(conn, user, e['id'], amount, quote['id'])
        payment_cost = Decimal(units) / pay_scale * (rate if mode == 'original' else 1)
        left -= payment_cost
        cost += payment_cost
        totals[code] = str(Decimal(totals.get(code, '0')) + Decimal(amount))
        items.append({'id': e['id'], 'description': e['description'], 'name': e['payer_name'], 'date': e['date'],
                      'amount': amount, 'currency': code, 'quote_id': quote['id'], 'entry_currency': e['currency'],
                      'credited': format(Decimal(credited) / (10 ** e['decimals']), f'.{e["decimals"]}f'),
                      'remaining': format(Decimal(remaining - credited) / (10 ** e['decimals']), f'.{e["decimals"]}f'),
                      'rate': quote['rate'], 'rate_date': quote['rate_date']})
        if credited < remaining:
            break
    if not items:
        fail('实际已入账资金不足以安排可登记的付款。请先记录真实进账，或到单笔账目手动登记。')
    return {'items': items, 'totals': totals, 'base': base, 'cost': str(cost), 'cash': str(cash), 'left': str(cash-cost),
            'mode': mode, 'snapshot': snapshot(conn), 'expires': int(time.time()) + 1800, 'note': note}
