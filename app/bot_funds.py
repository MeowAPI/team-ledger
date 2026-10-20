"""Telegram funds calculator and atomic, confirmed reimbursement batches."""
import hashlib
import json
import time
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING, ROUND_HALF_UP
from . import fx
from .db import settings
from .domain import currency, entry_view, fail, make_payment_quote, minor_amount, payment_preview, text_value, transition
from .bot_ui import amount_text, ask, button, draft_for, nav, new_draft, persist, say

PAGE_SIZE = 5
DEFAULT_NOTE = 'Telegram 按资金建议批量登记报销'


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


def pages(items, page):
    page = min(max(page, 0), max((len(items) - 1) // PAGE_SIZE, 0))
    return page, max((len(items) + PAGE_SIZE - 1) // PAGE_SIZE, 1), items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]


def paging(token, action, page, total):
    row = []
    if page:
        row.append(button('← 上一页', f'f:{token}:{action}:{page-1}'))
    if page + 1 < total:
        row.append(button('下一页 →', f'f:{token}:{action}:{page+1}'))
    return [row] if row else []


def show(conn, chat_id, token, payload, rates, page=0):
    conf = settings(conn)
    result = report(conn, rates, payload.get('scenario'))
    persist(conn, token, payload, 'funds_view')
    if not result or not result['complete']:
        say(conn, chat_id, '📊 暂时无法取得完整汇率，合并计算暂停。请稍后刷新。',
            [[button('刷新计算', f'f:{token}:refresh'), button('模拟新进账', f'f:{token}:calc')],
             [button('查看待报销项目', 'list:plan:0')], nav()], edit=True)
        return
    base = result['base']
    fmt = lambda value: amount_text(value, base, conf)
    lines = ['🧮 这笔进账，能报多少？ · ' + base]
    if payload.get('scenario'):
        scenario = payload['scenario']
        lines += ['⚗️ 模拟进账：' + amount_text(scenario['amount'], scenario['currency'], conf) + ' ≈ ' + fmt(result['extra']),
                  '以下含模拟收入，仅作试算；尚未入账。', '原账本可用资金：' + fmt(Decimal(result['cash']) - Decimal(result['extra']))]
    if result.get('distributed_balances'):
        lines.append('已分红收入（不计团队资金）：' + ' / '.join(amount_text(v, c, conf) for c, v in list(result['distributed_balances'].items())[:6]))
    for key, label in [('cash', '当前可用资金' if not payload.get('scenario') else '模拟后可用资金'),
                       ('due', '全部待报销'), ('reserve', '建议预留报销'), ('payable', '当前可安排付款'),
                       ('remaining', '预留报销后结余'), ('shortage', '还需补充的资金')]:
        lines.append(label + '：' + fmt(result[key]))
    projects = sorted([(m, e) for m in result['members'] for e in m['entries'] if Decimal(e['suggested']) > 0], key=lambda pair: (pair[1]['date'], pair[1]['id']))
    page, total, selected = pages(projects, page)
    lines.append(f'\n📌 建议付款项目 · 第 {page+1}/{total} 页 · 共 {len(projects)} 笔（最早发生优先）')
    rows = []
    for member, entry in selected:
        label = '剩余全额可安排' if entry['fully_covered'] else '可部分报销'
        lines.append(f'#{entry["id"]} {entry["description"][:60]}\n{member["name"][:40]} · {entry["date"]} · 剩余待报 ' +
                     amount_text(entry['amount'], entry['currency'], conf) + f'\n建议 {fmt(entry["suggested"])} · {label}')
        rows.append([button(f'查看 #{entry["id"]}', f'e:{entry["id"]}')])
    if not projects:
        lines.append('暂无可安排的付款项目。')
    dates = sorted({r['date'] for r in result['fx']['rates'].values() if r.get('date')})
    lines.append('\n可用资金 = 团队在账收入 − 实际已报销付款；优先覆盖待报销。\n每日参考汇率' + (' · ' + ' / '.join(dates) if dates else '') + '，仅供规划。')
    if result['fx'].get('warning'):
        lines.append(result['fx']['warning'])
    rows += paging(token, 'view', page, total)
    rows += [[button('模拟新进账', f'f:{token}:calc'), button('刷新 / 实际余额', f'f:{token}:refresh')]]
    if payload.get('scenario'):
        rows += [[button('＋ 将这笔真实进账记入账本', f'f:{token}:income')]]
    if Decimal(result['due']) > 0:
        rows += [[button('💳 按实际余额一键报销', f'f:{token}:batch')]]
    rows += [[button('全部待报销项目', 'list:plan:0')], nav()]
    say(conn, chat_id, '\n'.join(lines), rows, edit=True)


def start(conn, user, telegram_id, chat_id, rates):
    token, payload = new_draft(conn, user, telegram_id, chat_id, {'_flow': 'funds'}, 'funds_view')
    show(conn, chat_id, token, payload, rates)


def calculator_currencies(conn, chat_id, token, payload, page=0):
    conf = settings(conn)
    page, total, selected = pages(conf['currencies'], page)
    persist(conn, token, payload, 'funds_currency')
    rows = [[button(c['code'] + ' · ' + c['name'], f'f:{token}:cur:{c["code"]}')] for c in selected]
    rows += paging(token, 'currencies', page, total)
    rows += [[button('返回计算', f'f:{token}:view:0')], nav()]
    say(conn, chat_id, '选择本次模拟进账的币种。\n输入只用于试算，不会自动记入账本。', rows, edit=True)


def choose_mode(conn, chat_id, token, payload):
    payload.pop('scenario', None)
    payload.pop('_plan', None)
    conf = settings(conn)
    persist(conn, token, payload, 'funds_mode')
    say(conn, chat_id, '💳 按实际余额一键报销\n将重新计算已入账资金，按最早发生日期安排具体项目，资金不足时只安排可覆盖部分。\n\n选择本次实际付款币种，下一步核对清单。系统只登记已完成的转账。',
        [[button('按结算货币 · ' + conf['reference_currency'], f'f:{token}:mode:settlement')],
         [button('按各笔记账货币', f'f:{token}:mode:original')],
         [button('返回资金计算', f'f:{token}:view:0')]], edit=True)


def build_plan(conn, user, rates, mode):
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
    items, totals, cost = [], {}, Decimal(0)
    pending = sorted((e for e in entries if e['status'] == 'pending'), key=lambda e: (e['date'], e['id']))
    for e in pending:
        if left <= 0:
            break
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
            'mode': mode, 'snapshot': snapshot(conn), 'expires': int(time.time()) + 1800, 'note': DEFAULT_NOTE}


def show_plan(conn, chat_id, token, plan, page=0, done=False):
    conf = settings(conn)
    page, total, items = pages(plan['items'], page)
    lines = [f'✅ 已登记报销 · {len(plan["items"])} 笔' if done else f'💳 核对本次报销 · {len(plan["items"])} 笔',
             '实际付款合计：' + ' / '.join(amount_text(v, c, conf) for c, v in list(plan['totals'].items())[:6]),
             '折合结算参考：' + amount_text(plan['cost'], plan['base'], conf),
             '登记后结余参考：' + amount_text(plan['left'], plan['base'], conf),
             f'\n具体项目 · 第 {page+1}/{total} 页']
    if len(plan['totals']) > 6:
        lines.append(f'另有 {len(plan["totals"])-6} 个币种，逐笔付款请翻页核对。')
    for item in items:
        lines += [f'#{item["id"]} {item["description"][:60]}\n{item["name"][:40]} · {item["date"]}\n实际付款：' + amount_text(item['amount'], item['currency'], conf) +
                  '\n抵扣记账：' + amount_text(item['credited'], item['entry_currency'], conf) + ' · 剩余：' + amount_text(item['remaining'], item['entry_currency'], conf)]
        if item['currency'] != item['entry_currency']:
            lines.append(f'1 {item["entry_currency"]} = {item["rate"]} {item["currency"]} · {item["rate_date"]}')
    lines.append('\n付款备注：' + plan['note'])
    if not done:
        lines.append('请确认以上每笔已实际转账。点击确认后一次登记全部项目，未结清部分继续待报销。确认有效 30 分钟，账本变化后需重新计算。')
    rows = paging(token, 'receipt' if done else 'plan', page, total)
    if not done:
        rows += [[button(f'✅ 确认已转账，一键登记 {len(plan["items"])} 笔', f'f:{token}:commit')],
                 [button('填写付款备注', f'f:{token}:note'), button('重新选择币种', f'f:{token}:batch')],
                 [button('取消本次报销', f'f:{token}:cancel')]]
    else:
        rows += [[button('查看最新资金', 'funds'), button('查看待报销', 'list:team:0')], nav()]
    say(conn, chat_id, '\n'.join(lines), rows, edit=True)


def callback(conn, user, telegram_id, chat_id, data, rates):
    parts = data.split(':')
    if len(parts) < 3:
        fail('按钮无效，请重新打开资金计算。')
    draft = conn.execute('SELECT * FROM tg_drafts WHERE token=? AND telegram_id=? AND chat_id=? AND user_id=? AND expires>?',
                         (parts[1], telegram_id, chat_id, user['id'], int(time.time()))).fetchone()
    if not draft:
        fail('这不是你的当前计算，或它已过期，请使用 /funds 重新打开。')
    payload, token, step = json.loads(draft['payload']), draft['token'], draft['step']
    if payload.get('_flow') != 'funds':
        fail('请使用 /funds 重新打开资金计算。')
    action = parts[2]
    if action in ('plan', 'receipt', 'view', 'currencies'):
        if len(parts) != 4 or not parts[3].isdigit() or len(parts[3]) > 6:
            fail('分页参数无效')
        page = int(parts[3])
        if action == 'view':
            payload.pop('_plan', None)
            show(conn, chat_id, token, payload, rates, page)
        elif action == 'currencies' and step == 'funds_currency':
            calculator_currencies(conn, chat_id, token, payload, page)
        elif action == 'plan' and step == 'funds_confirm':
            show_plan(conn, chat_id, token, payload['_plan'], page)
        elif action == 'receipt' and step == 'funds_receipt':
            show_plan(conn, chat_id, token, payload['_receipt'], page, done=True)
        else:
            fail('步骤已变化，请重新打开资金计算。')
    elif action in ('refresh', 'cancel'):
        payload.pop('scenario', None)
        payload.pop('_plan', None)
        show(conn, chat_id, token, payload, rates)
    elif action == 'calc':
        payload.pop('_plan', None)
        calculator_currencies(conn, chat_id, token, payload)
    elif action == 'cur' and len(parts) == 4 and step == 'funds_currency':
        currency(settings(conn), parts[3])
        payload['scenario_currency'] = parts[3]
        persist(conn, token, payload, 'funds_amount')
        ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), 'funds_amount', f'请输入本次模拟进账金额（{parts[3]}）。只计算，不会入账。')
    elif action == 'batch':
        choose_mode(conn, chat_id, token, payload)
    elif action == 'mode' and len(parts) == 4 and parts[3] in ('original', 'settlement') and step == 'funds_mode':
        payload['_plan'] = build_plan(conn, user, rates, parts[3])
        persist(conn, token, payload, 'funds_confirm')
        show_plan(conn, chat_id, token, payload['_plan'])
    elif action == 'note' and step == 'funds_confirm':
        ask(conn, chat_id, draft, 'funds_note', '填写本批次付款方式 / 付款参考号，下一步核对并确认。')
    elif action == 'commit' and step == 'funds_receipt':
        show_plan(conn, chat_id, token, payload['_receipt'], done=True)
    elif action == 'commit' and step == 'funds_confirm':
        plan = payload['_plan']
        if time.time() >= plan['expires'] or snapshot(conn) != plan['snapshot']:
            fail('账本或结算币种已变化，或确认已过期。请用 /funds 重新计算并核对，尚未登记任何付款。')
        for item in plan['items']:
            payment_preview(conn, user, item['id'], item['amount'], item['quote_id'])
        for item in plan['items']:
            transition(conn, user, item['id'], 'pay', plan['note'], amount=item['amount'],
                       payment_key=f'pay:tg:batch:{token}:{item["id"]}', quote_id=item['quote_id'])
        payload['_receipt'] = payload.pop('_plan')
        persist(conn, token, payload, 'funds_receipt')
        show_plan(conn, chat_id, token, plan, done=True)
    elif action == 'income' and payload.get('scenario') and step == 'funds_view':
        from .bot_ui import preview
        from .config import today
        body = {'kind': 'income', 'amount': payload['scenario']['amount'], 'currency': payload['scenario']['currency'],
                'description': '团队进账', 'date': today(), 'note': '', 'status': 'posted'}
        new_token, body = new_draft(conn, user, telegram_id, chat_id, body)
        preview(conn, chat_id, new_token, body, rates)
    else:
        fail('此按钮不属于当前步骤，请使用 /funds 重新开始。')


def resume(conn, chat_id, draft, rates):
    payload, token, step = json.loads(draft['payload']), draft['token'], draft['step']
    if step == 'funds_confirm':
        show_plan(conn, chat_id, token, payload['_plan'])
    elif step == 'funds_receipt':
        show_plan(conn, chat_id, token, payload['_receipt'], done=True)
    elif step == 'funds_currency':
        calculator_currencies(conn, chat_id, token, payload)
    elif step == 'funds_mode':
        choose_mode(conn, chat_id, token, payload)
    elif step in ('funds_note', 'funds_amount'):
        ask(conn, chat_id, draft, step, '请填写付款备注。' if step == 'funds_note' else f'请输入模拟进账金额（{payload["scenario_currency"]}）。只计算，不入账。')
    else:
        show(conn, chat_id, token, payload, rates)


def text_input(conn, chat_id, draft, text, rates):
    payload, token, step = json.loads(draft['payload']), draft['token'], draft['step']
    if step == 'funds_amount':
        cur = currency(settings(conn), payload['scenario_currency'])
        minor_amount(text, cur['decimals'])
        payload['scenario'] = {'amount': text, 'currency': cur['code']}
        show(conn, chat_id, token, payload, rates)
    elif step == 'funds_note':
        payload['_plan']['note'] = text_value(text, '付款备注', 500)
        persist(conn, token, payload, 'funds_confirm')
        show_plan(conn, chat_id, token, payload['_plan'])
    else:
        say(conn, chat_id, '请点击当前计算的按钮继续，或使用 /funds 重新计算。', [nav()])
