"""Telegram funds calculator and atomic, confirmed reimbursement batches."""
import json
import time
from decimal import Decimal
from .db import settings
from .domain import currency, fail, minor_amount, payment_preview, text_value, transition
from .funds_plan import build_plan, report, snapshot
from .bot_ui import amount_text, ask, button, draft_for, entry_description, nav, new_draft, persist, say

PAGE_SIZE = 5


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
    result = report(conn, rates, payload.get('scenario'), payload.get('reimbursement'))
    persist(conn, token, payload, 'funds_view')
    if not result or not result['complete']:
        say(conn, chat_id, '📊 暂时无法取得完整汇率，合并计算暂停。请稍后刷新。',
            [[button('刷新计算', f'f:{token}:refresh'), button('模拟新进账', f'f:{token}:calc')],
             [button('查看待报销项目', 'list:plan:0')], nav()], edit=True)
        return
    base = result['base']
    fmt = lambda value: amount_text(value, base, conf)
    lines = ['🧮 这笔进账，能报多少？ · ' + base]
    choice = result['reimbursement']
    lines.append('本次报销：' + ({'auto': '自动安排 · 最早发生优先',
                  'budget': '指定预算 ' + fmt(choice['budget'] or '0'),
                  'manual': f'仅安排勾选的 {len(choice["entry_ids"])} 个项目'}[choice['mode']]))
    if payload.get('scenario'):
        scenario = payload['scenario']
        lines += ['⚗️ 模拟进账：' + amount_text(scenario['amount'], scenario['currency'], conf) + ' ≈ ' + fmt(result['extra']),
                  '以下含模拟收入，仅作试算；尚未入账。', '原账本可用资金：' + fmt(Decimal(result['cash']) - Decimal(result['extra']))]
    for key, label in [('cash', '当前可用资金' if not payload.get('scenario') else '模拟后可用资金'),
                       ('due', '全部待报销'), ('reserve', '本次安排报销'), ('payable', '当前可安排付款'),
                       ('remaining', '本次安排后结余'), ('unplanned', '本次安排后仍待报销'),
                       ('dividend_paid', '累计盈余分红'), ('dividend_available', '真实余额可分红'), ('shortage', '还需补充的资金')]:
        lines.append(label + '：' + fmt(result[key]))
    projects = sorted([(m, e) for m in result['members'] for e in m['entries'] if Decimal(e['suggested']) > 0], key=lambda pair: (pair[1]['date'], pair[1]['id']))
    page, total, selected = pages(projects, page)
    lines.append(f'\n📌 建议付款项目 · 第 {page+1}/{total} 页 · 共 {len(projects)} 笔（最早发生优先）')
    rows = []
    for member, entry in selected:
        label = '剩余全额可安排' if entry['fully_covered'] else '可部分报销'
        lines.append(f'#{entry["id"]} {entry_description(entry)[:60]}\n{member["name"][:40]} · {entry["date"]} · 剩余待报 ' +
                     amount_text(entry['amount'], entry['currency'], conf) + f'\n建议 {fmt(entry["suggested"])} · {label}')
        rows.append([button(f'查看 #{entry["id"]}', f'e:{entry["id"]}')])
    if not projects:
        lines.append('暂无可安排的付款项目。')
    dates = sorted({r['date'] for r in result['fx']['rates'].values() if r.get('date')})
    lines.append('\n可用资金 = 团队收入 − 已报销 − 已分红；待报销未结清也可分红，分红会减少后续可报销资金。\n每日参考汇率' + (' · ' + ' / '.join(dates) if dates else '') + '，仅供规划。')
    if result['fx'].get('warning'):
        lines.append(result['fx']['warning'])
    rows += paging(token, 'view', page, total)
    rows += [[button('模拟新进账', f'f:{token}:calc'), button('刷新 / 实际余额', f'f:{token}:refresh')]]
    rows += [[button('设置报销预算', f'f:{token}:budget'), button('选择报销项目', f'f:{token}:pick:0')],
             [button('恢复自动安排', f'f:{token}:auto')]]
    if payload.get('scenario'):
        rows += [[button('＋ 将这笔真实进账记入账本', f'f:{token}:income')]]
    if Decimal(result['due']) > 0:
        rows += [[button('💳 按实际余额一键报销', f'f:{token}:batch')]]
    rows += [[button('盈余分红 / 分红记录', 'dividends'), button('全部待报销项目', 'list:plan:0')], nav()]
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
    say(conn, chat_id, '💳 按实际余额一键报销\n将重新计算已入账资金，沿用本次报销预算或所选项目，按最早发生日期安排，资金不足时只安排可覆盖部分。\n\n选择本次实际付款币种，下一步核对清单。系统只登记已完成的转账。',
        [[button('按结算货币 · ' + conf['reference_currency'], f'f:{token}:mode:settlement')],
         [button('按各笔记账货币', f'f:{token}:mode:original')],
         [button('返回资金计算', f'f:{token}:view:0')]], edit=True)


def choose_projects(conn, chat_id, token, payload, rates, page=0):
    result = report(conn, rates, reimbursement=payload.get('reimbursement'))
    if not result or not result['complete']:
        fail('暂无完整汇率，请先刷新资金计算。')
    projects = sorted([(m, e) for m in result['members'] for e in m['entries']], key=lambda pair: (pair[1]['date'], pair[1]['id']))
    page, total, items = pages(projects, page)
    choice = payload['reimbursement']
    selected = set(choice['entry_ids'])
    conf = settings(conn)
    lines = [f'☑️ 选择本次报销项目 · 第 {page+1}/{total} 页', f'已选 {len(selected)} 项；未选项目继续待报销。']
    rows = []
    for member, entry in items:
        lines.append(f'\n#{entry["id"]} {entry_description(entry)[:60]}\n{member["name"][:40]} · ' +
                     amount_text(entry['amount'], entry['currency'], conf))
        rows.append([button(('☑ ' if entry['id'] in selected else '☐ ') + f'#{entry["id"]} {entry_description(entry)[:28]}',
                            f'f:{token}:select:{entry["id"]}:{page}')])
    if not items:
        lines.append('暂无待报销项目。')
    rows += paging(token, 'pick', page, total)
    rows += [[button('完成选择，查看安排', f'f:{token}:view:0')], [button('恢复自动安排', f'f:{token}:auto')]]
    persist(conn, token, payload, 'funds_projects')
    say(conn, chat_id, '\n'.join(lines), rows, edit=True)


def show_plan(conn, chat_id, token, plan, page=0, done=False):
    conf = settings(conn)
    page, total, items = pages(plan['items'], page)
    lines = [f'✅ 已登记报销 · {len(plan["items"])} 笔' if done else f'💳 核对本次报销 · {len(plan["items"])} 笔',
             '实际付款合计：' + ' / '.join(amount_text(v, c, conf) for c, v in list(plan['totals'].items())[:6]),
             '折合结算参考：' + amount_text(plan['cost'], plan['base'], conf),
             '登记后结余参考：' + amount_text(plan['left'], plan['base'], conf),
             f'\n具体项目 · 第 {page+1}/{total} 页']
    choice = plan.get('reimbursement', {})
    if choice.get('mode') == 'budget':
        lines.insert(1, '本次报销预算：' + amount_text(choice['budget'], plan['base'], conf))
    elif choice.get('mode') == 'manual':
        lines.insert(1, f'仅安排所选项目 · {len(choice["entry_ids"])} 项')
    if len(plan['totals']) > 6:
        lines.append(f'另有 {len(plan["totals"])-6} 个币种，逐笔付款请翻页核对。')
    for item in items:
        lines += [f'#{item["id"]} {entry_description(item)[:60]}\n{item["name"][:40]} · {item["date"]}\n实际付款：' + amount_text(item['amount'], item['currency'], conf) +
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
    if action == 'budget' and step in ('funds_view', 'funds_projects'):
        payload.pop('_plan', None)
        payload['budget_currency'] = settings(conn)['reference_currency']
        persist(conn, token, payload, 'funds_budget')
        ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), 'funds_budget',
            f'请输入本次用于报销的金额（{settings(conn)["reference_currency"]}）。最多使用此预算，填 0 暂不报销。')
    elif action == 'auto':
        payload.pop('reimbursement', None)
        payload.pop('_plan', None)
        show(conn, chat_id, token, payload, rates)
    elif action == 'pick' and len(parts) == 4 and parts[3].isdigit() and len(parts[3]) <= 6:
        payload.pop('_plan', None)
        if payload.get('reimbursement', {}).get('mode') != 'manual':
            payload['reimbursement'] = {'mode': 'manual', 'entry_ids': []}
        choose_projects(conn, chat_id, token, payload, rates, int(parts[3]))
    elif action == 'select' and len(parts) == 5 and all(p.isdigit() and len(p) <= 12 for p in parts[3:]) and step == 'funds_projects':
        entry_id, page = int(parts[3]), int(parts[4])
        pending = conn.execute("SELECT 1 FROM entries WHERE id=? AND kind='expense' AND status='pending'", (entry_id,)).fetchone()
        if not pending:
            fail('该项目不再待报销，请重新打开资金计算。', 409)
        ids = set(payload['reimbursement']['entry_ids'])
        ids.symmetric_difference_update({entry_id})
        payload['reimbursement']['entry_ids'] = sorted(ids)
        choose_projects(conn, chat_id, token, payload, rates, page)
    elif action in ('plan', 'receipt', 'view', 'currencies'):
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
        payload['_plan'] = build_plan(conn, user, rates, parts[3], reimbursement=payload.get('reimbursement'))
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
    elif step == 'funds_projects':
        choose_projects(conn, chat_id, token, payload, rates)
    elif step in ('funds_note', 'funds_amount', 'funds_budget'):
        prompt = '请填写付款备注。' if step == 'funds_note' else (
            f'请输入本次报销预算（{settings(conn)["reference_currency"]}），填 0 暂不报销。' if step == 'funds_budget'
            else f'请输入模拟进账金额（{payload["scenario_currency"]}）。只计算，不入账。')
        ask(conn, chat_id, draft, step, prompt)
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
    elif step == 'funds_budget':
        conf = settings(conn)
        if payload.get('budget_currency', conf['reference_currency']) != conf['reference_currency']:
            fail('结算币种已变化，请重新打开资金计算并填写预算', 409)
        cur = currency(conf, conf['reference_currency'])
        minor_amount(text, cur['decimals'], allow_zero=True)
        payload['reimbursement'] = {'mode': 'budget', 'budget': text, 'currency': cur['code']}
        show(conn, chat_id, token, payload, rates)
    else:
        say(conn, chat_id, '请点击当前计算的按钮继续，或使用 /funds 重新计算。', [nav()])
