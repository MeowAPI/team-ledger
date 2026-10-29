import json
import os
import time
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from .db import db, settings
from .domain import currency, fail, minor_amount
from .config import today
from . import reimbursement_plan


def fetch_rates(base, codes):
    url = os.getenv('FX_API_URL', 'https://api.frankfurter.app/latest')
    is_v2 = '/v2/' in url
    query = urllib.parse.urlencode({'base' if is_v2 else 'from': base, 'quotes' if is_v2 else 'to': ','.join(sorted(codes))})
    request = urllib.request.Request(url + ('&' if '?' in url else '?') + query, headers={'User-Agent': 'TogetherLedger/1.0'})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read(1_000_000), parse_float=Decimal)
    if isinstance(payload, dict) and payload.get('base') == base and isinstance(payload.get('rates'), dict):
        payload = [{'base':base,'quote':quote,'rate':rate,'date':payload.get('date')} for quote,rate in payload['rates'].items()]
    if not isinstance(payload, list):
        raise ValueError('Invalid exchange-rate response')
    result = {}
    for row in payload:
        if row.get('base') == base and row.get('quote') in codes:
            rate = Decimal(str(row['rate']))
            if not rate.is_finite() or rate <= 0:
                raise ValueError('Invalid exchange rate')
            from datetime import date
            rate_date = date.fromisoformat(str(row['date'])).isoformat()
            result[row['quote']] = {'rate': str(Decimal(1) / rate), 'date': rate_date}
    if set(result) != set(codes):
        raise ValueError('Some currencies are unavailable')
    return result


def cached_rates(base, codes):
    """Read-only rates for use inside an open write transaction (never fetches)."""
    codes = set(codes) - {base}
    result = {'base': base, 'rates': {base: {'rate': '1', 'date': None}},
              'source': 'Frankfurter · 每日参考汇率', 'stale': False, 'missing': [], 'warning': None}
    if not codes:
        return result
    with db() as conn:
        row = conn.execute('SELECT * FROM fx_cache WHERE base=?', (base,)).fetchone()
    cached = json.loads(row['payload']) if row else {}
    fresh = row and time.time() - row['fetched_at'] < float(os.getenv('FX_CACHE_HOURS', '12')) * 3600
    result['rates'].update({c: cached[c] for c in codes if c in cached})
    result['missing'] = sorted(codes - set(cached))
    result['stale'] = not fresh
    if not fresh:
        result['warning'] = '汇率缓存已过期或尚未获取，跨币种金额仅供参考。'
    elif result['missing']:
        result['warning'] = '汇率缓存缺少部分币种，跨币种金额暂不可用。'
    return result


def rates_for(base, codes, force=False):
    codes = set(codes) - {base}
    result = {'base': base, 'rates': {base: {'rate': '1', 'date': None}},
              'source': 'Frankfurter · 每日参考汇率', 'stale': False, 'missing': [], 'warning': None}
    if not codes:
        return result
    with db() as conn:
        row = conn.execute('SELECT * FROM fx_cache WHERE base=?', (base,)).fetchone()
    cached = json.loads(row['payload']) if row else {}
    fresh = row and time.time() - row['fetched_at'] < float(os.getenv('FX_CACHE_HOURS', '12')) * 3600
    if fresh and codes <= set(cached) and not force:
        result['rates'].update({c: cached[c] for c in codes})
        return result
    try:
        rates = fetch_rates(base, codes)
        # A cache record represents a single fetch; do not refresh timestamps on old pairs.
        with db(write=True) as conn:
            conn.execute('INSERT OR REPLACE INTO fx_cache VALUES (?,?,?)', (base, json.dumps(rates), int(time.time())))
        result['rates'].update(rates)
    except Exception:
        result['stale'] = bool(cached)
        result['rates'].update({c: cached[c] for c in codes if c in cached})
        result['missing'] = sorted(codes - set(cached))
        result['warning'] = '汇率接口暂不可用，正在使用上次缓存的参考汇率。' if cached else '汇率接口暂不可用，跨币种计算暂停。原币种账目仍可正常记录。'
    return result


def calculate(entries, conf, fx, scenario=None, payments=None, dividends=None, reimbursement=None):
    base = fx['base']
    base_decimals = currency(conf, base)['decimals']
    quantum = Decimal(1).scaleb(-base_decimals)

    def formatted(value):
        return format(value.quantize(quantum, rounding=ROUND_HALF_UP), f'.{base_decimals}f')

    def convert(entry, units=None):
        return Decimal(entry['amount_minor'] if units is None else units) / (10 ** entry['decimals']) * Decimal(fx['rates'][entry['currency']]['rate'])

    payments = payments or []
    dividends = dividends or []
    balances = {}
    for entry, factor in [(e, 1) for e in entries if e['kind'] == 'income' and e['status'] == 'posted'] + [(p, -1) for p in payments + dividends]:
        key = entry['currency']
        balances[key] = balances.get(key, Decimal(0)) + factor * Decimal(entry['amount_minor']) / (10 ** entry['decimals'])
    dividend_balances = {}
    for dividend in dividends:
        code = dividend['currency']
        dividend_balances[code] = str(Decimal(dividend_balances.get(code, '0')) + Decimal(dividend['amount_minor']) / (10 ** dividend['decimals']))
    choice = reimbursement_plan.normalize(entries, conf, base, reimbursement)
    common = {'reimbursement': choice, 'dividend_balances': dividend_balances, 'base': base, 'fx': fx, 'balances': {k: str(v) for k, v in balances.items()},
              'complete': not fx['missing'], 'scenario': scenario, 'members': []}
    if fx['missing']:
        return common
    income = sum((convert(e) for e in entries if e['kind'] == 'income' and e['status'] == 'posted'), Decimal(0))
    today_income = sum((convert(e) for e in entries if e['kind'] == 'income' and e['status'] == 'posted' and e['date'] == today()), Decimal(0))
    paid = sum((convert(p) for p in payments), Decimal(0))
    dividend_paid = sum((convert(d) for d in dividends), Decimal(0))
    due_entries = sorted((e for e in entries if e['status'] == 'pending'), key=lambda e: (e['date'], e['id']))
    due = sum((convert(e, e['amount_minor'] - e['reimbursed_minor']) for e in due_entries), Decimal(0))
    extra = Decimal(0)
    if scenario:
        cur = currency(conf, scenario['currency'])
        units = minor_amount(scenario['amount'], cur['decimals'])
        extra = Decimal(units) / (10 ** cur['decimals']) * Decimal(fx['rates'][cur['code']]['rate'])
    actual_cash = income - paid - dividend_paid
    cash = actual_cash + extra
    available = max(cash, Decimal(0))
    selected_ids = set(choice['entry_ids']) if choice['mode'] == 'manual' else {e['id'] for e in due_entries}
    selected_due = sum((convert(e, e['amount_minor'] - e['reimbursed_minor']) for e in due_entries if e['id'] in selected_ids), Decimal(0))
    reserve = reimbursement_plan.limit(choice, available, selected_due)
    payable = reserve
    groups = {}
    left = payable
    # Unselected projects stay visible and outstanding, with no allocation.
    for entry in due_entries:
        key = entry['payer_id']
        if key not in groups:
            groups[key] = {'id': key, 'name': entry['payer_name'], 'due': Decimal(0), 'suggested': Decimal(0), 'entries': []}
        amount = convert(entry, entry['amount_minor'] - entry['reimbursed_minor'])
        groups[key]['due'] += amount
        selected = entry['id'] in selected_ids
        portion = min(left, amount) if selected else Decimal(0)
        groups[key]['suggested'] += portion
        left -= portion
        original = Decimal(entry['amount_minor'] - entry['reimbursed_minor']) / (10 ** entry['decimals'])
        groups[key]['entries'].append({
            'id': entry['id'],
            'selected': selected,
            'description': entry['description'],
            'date': entry['date'],
            'currency': entry['currency'],
            'amount': format(original, f'.{entry["decimals"]}f'),
            'original_amount': format(Decimal(entry['amount_minor']) / (10 ** entry['decimals']), f'.{entry["decimals"]}f'),
            'reimbursed_amount': format(Decimal(entry['reimbursed_minor']) / (10 ** entry['decimals']), f'.{entry["decimals"]}f'),
            'settlement': formatted(amount),
            'suggested': formatted(portion),
            'fully_covered': portion >= amount,
        })
    common.update({k: formatted(v) for k, v in dict(income=income, today_income=today_income, paid=paid, dividend_paid=dividend_paid,
                   dividend_available=max(actual_cash, Decimal(0)), actual_cash=actual_cash, cash=cash, due=due, selected_due=selected_due,
                   reserve=reserve, payable=payable, remaining=cash-reserve, unplanned=due-reserve,
                   shortage=max(due-available, Decimal(0)), extra=extra).items()})
    common['members'] = [{**g, **{k: formatted(g[k]) for k in ('due', 'suggested')}} for g in groups.values()]
    return common


def report(base=None, scenario=None, force=False, reimbursement=None):
    with db() as conn:
        conf = settings(conn)
        base = base or conf['reference_currency']
        currency(conf, base)
        entries = [dict(e) for e in conn.execute('SELECT e.*,u.name payer_name FROM entries e JOIN users u ON u.id=e.payer_id')]
        payments = [dict(p) for p in conn.execute('SELECT * FROM payments')]
        dividends = [dict(d) for d in conn.execute('SELECT * FROM dividends')]
    # Ignore drafts and voided entries when looking up rates.
    codes = {e['currency'] for e in entries if (e['kind'] == 'income' and e['status'] == 'posted') or e['status'] in ('pending', 'reimbursed')}
    codes.update(p['currency'] for p in payments)
    codes.update(d['currency'] for d in dividends)
    if scenario:
        currency(conf, scenario['currency'])
        minor_amount(scenario['amount'], currency(conf, scenario['currency'])['decimals'])
        codes.add(scenario['currency'])
    # Reject invalid choices before requesting external exchange rates.
    reimbursement = reimbursement_plan.normalize(entries, conf, base, reimbursement)
    return calculate(entries, conf, rates_for(base, codes, force), scenario, payments, dividends, reimbursement)
