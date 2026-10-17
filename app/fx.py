import json
import os
import time
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from .db import db, settings
from .domain import currency, fail, minor_amount
from .config import today


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


def calculate(entries, conf, fx, scenario=None):
    base = fx['base']
    base_decimals = currency(conf, base)['decimals']
    quantum = Decimal(1).scaleb(-base_decimals)

    def formatted(value):
        return format(value.quantize(quantum, rounding=ROUND_HALF_UP), f'.{base_decimals}f')

    def convert(entry):
        return Decimal(entry['amount_minor']) / (10 ** entry['decimals']) * Decimal(fx['rates'][entry['currency']]['rate'])

    balances = {}
    for entry in entries:
        if (entry['kind'] == 'income' and entry['status'] == 'posted') or entry['status'] == 'reimbursed':
            factor = 1 if entry['kind'] == 'income' else -1
            key = entry['currency']
            balances[key] = balances.get(key, Decimal(0)) + factor * Decimal(entry['amount_minor']) / (10 ** entry['decimals'])
    common = {'base': base, 'fx': fx, 'balances': {k: str(v) for k, v in balances.items()},
              'complete': not fx['missing'], 'scenario': scenario, 'members': []}
    if fx['missing']:
        return common
    income = sum((convert(e) for e in entries if e['kind'] == 'income' and e['status'] == 'posted'), Decimal(0))
    today_income = sum((convert(e) for e in entries if e['kind'] == 'income' and e['status'] == 'posted' and e['date'] == today()), Decimal(0))
    paid = sum((convert(e) for e in entries if e['status'] == 'reimbursed'), Decimal(0))
    due_entries = sorted((e for e in entries if e['status'] == 'pending'), key=lambda e: (e['date'], e['id']))
    due = sum((convert(e) for e in due_entries), Decimal(0))
    extra = Decimal(0)
    if scenario:
        cur = currency(conf, scenario['currency'])
        units = minor_amount(scenario['amount'], cur['decimals'])
        extra = Decimal(units) / (10 ** cur['decimals']) * Decimal(fx['rates'][cur['code']]['rate'])
    cash = income - paid + extra
    available = max(cash, Decimal(0))
    reserve = min(available, due)
    payable = reserve
    groups = {}
    left = payable
    # Every pending reimbursement is eligible, in expense-date order.
    for entry in due_entries:
        key = entry['payer_id']
        if key not in groups:
            groups[key] = {'id': key, 'name': entry['payer_name'], 'due': Decimal(0), 'suggested': Decimal(0)}
        amount = convert(entry)
        groups[key]['due'] += amount
        portion = min(left, amount)
        groups[key]['suggested'] += portion
        left -= portion
    common.update({k: formatted(v) for k, v in dict(income=income, today_income=today_income, paid=paid, cash=cash, due=due,
                   reserve=reserve, payable=payable, remaining=cash-reserve, shortage=max(due-available, Decimal(0)), extra=extra).items()})
    common['members'] = [{**g, **{k: formatted(g[k]) for k in ('due', 'suggested')}} for g in groups.values()]
    return common


def report(base=None, scenario=None, force=False):
    with db() as conn:
        conf = settings(conn)
        base = base or conf['reference_currency']
        currency(conf, base)
        entries = [dict(e) for e in conn.execute('SELECT e.*,u.name payer_name FROM entries e JOIN users u ON u.id=e.payer_id')]
    # Ignore drafts and voided entries when looking up rates.
    codes = {e['currency'] for e in entries if (e['kind'] == 'income' and e['status'] == 'posted') or e['status'] in ('pending', 'reimbursed')}
    if scenario:
        currency(conf, scenario['currency'])
        minor_amount(scenario['amount'], currency(conf, scenario['currency'])['decimals'])
        codes.add(scenario['currency'])
    return calculate(entries, conf, rates_for(base, codes, force), scenario)
