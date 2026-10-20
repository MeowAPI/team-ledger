"""Telegram menus, editable drafts and confirmed ledger operations."""
import hashlib
import json
import os
import time
import secrets
from contextvars import ContextVar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from . import config
from .db import settings
from .domain import (add_entry, currency, edit_entry, entry_view, fail, minor_amount, normalized_entry,
                     text_value, transition, make_payment_quote, payment_preview)

DELIVERY = ContextVar('telegram_delivery', default={})
PAGE_SIZE = 5
STATUS = {'pending': '待报销', 'reimbursed': '已报销', 'draft': '草稿', 'posted': '团队在账', 'distributed': '已分红', 'voided': '已作废'}
COMMANDS = [('start', '打开团队账本菜单'), ('book', '记录成员垫付'), ('income', '记录团队收入'),
            ('ai', '自然语言记账'), ('recent', '最近账目与详情'), ('summary', '我的待报销明细'),
            ('team', '团队待报销明细'), ('funds', '资金计算、模拟进账与一键报销'), ('today', '今日收支'),
            ('month', '本月收支'), ('cancel', '取消当前填写'), ('help', '使用帮助')]


def queue(conn, method, payload):
    conn.execute('INSERT INTO tg_outbox(method,payload) VALUES (?,?)', (method, json.dumps(payload, ensure_ascii=False)))


def button(text, data):
    return {'text': text, 'callback_data': data}


def nav():
    return [button('🏠 主菜单', 'home'), button('＋ 再记一笔', 'new')]


def web_button():
    url = os.getenv('PUBLIC_URL', '')
    return {'text': '打开 Web 账本', 'url': url} if url.startswith(('https://', 'http://')) else None


def say(conn, chat_id, text, buttons=None, edit=False, prompt=None):
    ctx = DELIVERY.get()
    payload, method = {'chat_id': chat_id, 'text': text}, 'sendMessage'
    if edit and ctx.get('callback') and ctx.get('message_id'):
        method = 'editMessageText'
        payload['message_id'] = ctx['message_id']
        payload['_thread_id'] = ctx.get('thread_id')
    elif ctx.get('thread_id'):
        payload['message_thread_id'] = ctx['thread_id']
    if buttons:
        payload['reply_markup'] = {'inline_keyboard': buttons}
    if prompt:
        payload['reply_markup'] = {'force_reply': True, 'selective': True, 'input_field_placeholder': '回复此消息填写，/cancel 取消'}
        if ctx.get('user_message_id'):
            payload['reply_parameters'] = {'message_id': ctx['user_message_id'], 'allow_sending_without_reply': True}
        payload['_prompt'] = prompt
    queue(conn, method, payload)


def amount_text(value, code, conf):
    cur = currency(conf, code)
    value = Decimal(str(value)).quantize(Decimal(1).scaleb(-cur['decimals']), rounding=ROUND_HALF_UP)
    return f'{cur["symbol"]}{value:,.{cur["decimals"]}f} {code}'


def original(entry, conf):
    return amount_text(Decimal(entry['amount_minor']) / (10 ** entry['decimals']), entry['currency'], conf)


def entry_status(entry):
    return '部分报销' if entry['status'] == 'pending' and entry['reimbursed_minor'] else STATUS[entry['status']]


def payment_totals(entry, conf):
    return f'累计已报：{amount_text(entry["reimbursed_amount"],entry["currency"],conf)}\n剩余待报：{amount_text(entry["remaining_amount"],entry["currency"],conf)}'


def reference(value, code, conf, rates):
    base = conf['reference_currency']
    if code == base:
        return ''
    row = (rates or {}).get('rates', {}).get(code)
    if not row:
        return f'结算参考：{base} 汇率暂不可用'
    converted = amount_text(Decimal(str(value)) * Decimal(row['rate']), base, conf)
    suffix = ' · 缓存' if rates.get('stale') else ''
    return f'≈ {converted} · {row["date"]}{suffix}'


def menu(conn, user, chat_id):
    conf = settings(conn)
    count = conn.execute("SELECT COUNT(*) FROM entries WHERE payer_id=? AND status='pending'", (user['id'],)).fetchone()[0]
    rows = [[button('💸 成员垫付', 'new'), button('💰 团队收入', 'newincome')],
            [button('✨ AI 记账', 'ai'), button('📋 最近账目', 'list:recent:0')],
            [button(f'🙋 我的待报销 · {count}', 'list:mine:0'), button('👥 团队待报销', 'list:team:0')],
            [button('📊 资金与付款项目', 'funds'), button('📅 今日收支', 'period:today')],
            [button('本月收支', 'period:month'), button('使用帮助', 'help')]]
    draft = conn.execute("SELECT token FROM tg_drafts WHERE user_id=? AND chat_id=? AND expires>? AND step!='funds_receipt'",
                         (user['id'], chat_id, int(time.time()))).fetchone()
    if draft:
        rows.insert(1, [button('继续当前填写', f'd:{draft["token"]}:resume'), button('取消当前填写', f'd:{draft["token"]}:cancel')])
    if web_button():
        rows.append([web_button()])
    say(conn, chat_id, f'📒 {conf["team_name"]}\n{user["name"]}，你的团队账本已连接。\n\n'
        f'默认币种 {conf["default_currency"]} · 结算参考 {conf["reference_currency"]}\n'
        '记账后直接待报销，付款后标记已报销。\n选择下方功能，或私聊直接描述一笔收支。', rows, edit=True)


def help_menu(conn, chat_id):
    say(conn, chat_id, '📖 使用帮助\n\n'
        '记账：点成员垫付 / 团队收入。预览可修改金额、币种、日期、用途、备注和成员。\n'
        'AI：私聊直接说「昨天打车花了35元」，群里使用 /ai 记账内容；核对预览后才入账。\n'
        '查账：最近账目、个人与团队待报销均可翻页，点项目查看详情。\n'
        '报销：待报销详情可分次登记；/funds 可模拟进账、查看具体项目，按实际余额一键登记多笔报销。\n'
        '一键报销：选择记账 / 结算货币，核对全部项目后确认已转账。模拟收入不会自动入账，也不会用于实际付款。\n'
        '群聊：请回复机器人弹出的输入提示，普通聊天不会触发记账。成员草稿互相独立。\n'
        '取消：/cancel；返回菜单：/start。凭证文件请到 Web 上传。', [nav()], edit=True)


def new_draft(conn, user, telegram_id, chat_id, payload=None, step=None):
    conn.execute('DELETE FROM tg_drafts WHERE telegram_id=? AND chat_id=?', (telegram_id, chat_id))
    token = secrets.token_hex(5)
    step = step or ('confirm' if payload else 'currency')
    payload = dict(payload) if payload else {'kind': 'expense', 'date': config.today(), 'note': '', 'status': 'pending'}
    payload.setdefault('payer_id', user['id'])
    conn.execute('INSERT INTO tg_drafts VALUES (?,?,?,?,?,?,?)',
                 (token, telegram_id, chat_id, user['id'], step, json.dumps(payload, ensure_ascii=False), int(time.time()) + 3600))
    return token, payload


def draft_for(conn, telegram_id, chat_id):
    return conn.execute('SELECT * FROM tg_drafts WHERE telegram_id=? AND chat_id=? AND expires>?',
                        (telegram_id, chat_id, int(time.time()))).fetchone()


def persist(conn, token, data, step):
    conn.execute('UPDATE tg_drafts SET step=?,payload=?,expires=? WHERE token=?',
                 (step, json.dumps(data, ensure_ascii=False), int(time.time()) + 3600, token))


def ask(conn, chat_id, draft, step, text):
    persist(conn, draft['token'], json.loads(draft['payload']), step)
    name = conn.execute('SELECT name FROM users WHERE id=?', (draft['user_id'],)).fetchone()['name']
    say(conn, chat_id, f'{name}，{text}\n\n群聊中请回复这条提示。/cancel 可取消。',
        prompt={'token': draft['token'], 'telegram_id': draft['telegram_id'], 'step': step})
    row = conn.execute('SELECT id,payload FROM tg_outbox ORDER BY id DESC LIMIT 1').fetchone()
    payload = json.loads(row['payload'])
    payload['entities'] = [{'type': 'text_mention', 'offset': 0, 'length': len(name.encode('utf-16-le')) // 2,
                            'user': {'id': int(draft['telegram_id']), 'is_bot': False, 'first_name': name}}]
    conn.execute('UPDATE tg_outbox SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), row['id']))


def show_currencies(conn, chat_id, token, conf):
    buttons = [button(c['code'] + ' ' + c['name'], f'd:{token}:cur:{c["code"]}') for c in conf['currencies']]
    say(conn, chat_id, '1/3 · 选择币种', [buttons[i:i+2] for i in range(0, len(buttons), 2)] + [[button('取消', f'd:{token}:cancel')]], edit=True)


def preview(conn, chat_id, token, data, rates=None):
    conf = settings(conn)
    payer = conn.execute('SELECT name FROM users WHERE id=?', (data.get('payer_id'),)).fetchone()
    kind = '团队收入' if data['kind'] == 'income' else '成员垫付 · 待报销'
    lines = [f'🧾 确认{kind}', f'用途：{data["description"]}', f'金额：{amount_text(data["amount"],data["currency"],conf)}']
    converted = reference(data['amount'], data['currency'], conf, rates)
    if converted:
        lines.append(converted)
    if data['kind'] == 'income':
        lines.append('收入归属：已分红到个人账户，不计团队余额。' if data.get('status') == 'distributed' else '收入归属：团队在账，可用于报销。')
    lines += [f'日期：{data["date"]}', f'成员：{payer["name"] if payer else "当前成员"}', f'备注：{data.get("note") or "未填写"}', '\n核对后确认才入账。']
    say(conn, chat_id, '\n'.join(lines),
        [[button('✅ 保存修改' if data.get('_edit_id') else '✅ 确认入账', f'd:{token}:save')],
         [button('金额', f'd:{token}:edit:amount'), button('币种', f'd:{token}:edit:currency'), button('用途', f'd:{token}:edit:description')],
         [button('日期', f'd:{token}:edit:date'), button('备注', f'd:{token}:edit:note'), button('成员', f'd:{token}:edit:payer')],
         *([[button('切换为团队在账' if data.get('status') == 'distributed' else '切换为已分红收入', f'd:{token}:incomestate')]] if data['kind'] == 'income' else []),
         [button('取消', f'd:{token}:cancel'), button('主菜单', 'home')]], edit=True)


def list_entries(conn, user, chat_id, scope='recent', page=0, rates=None):
    if scope not in ('recent', 'mine', 'team', 'plan'):
        fail('列表不存在')
    params, where = [], ''
    if scope == 'mine':
        where, params = "WHERE payer_id=? AND status='pending'", [user['id']]
    elif scope in ('team', 'plan'):
        where = "WHERE status='pending'"
    count = conn.execute('SELECT COUNT(*) FROM entries ' + where, params).fetchone()[0]
    page = min(max(page, 0), max((count - 1) // PAGE_SIZE, 0))
    order = 'date,id' if scope == 'plan' else 'date DESC,id DESC'
    ids = conn.execute(f'SELECT id FROM entries {where} ORDER BY {order} LIMIT ? OFFSET ?', params + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
    conf = settings(conn)
    title = {'recent': '📋 最近账目', 'mine': '🙋 我的待报销', 'team': '👥 团队待报销', 'plan': '📌 待报销项目 · 最早日期优先'}[scope]
    lines = [f'{title}\n第 {page + 1} / {max((count + PAGE_SIZE - 1) // PAGE_SIZE, 1)} 页 · 共 {count} 笔']
    if scope in ('mine', 'team'):
        sums = conn.execute('SELECT currency,decimals,SUM(amount_minor-reimbursed_minor) amount FROM entries ' + where + ' GROUP BY currency,decimals', params).fetchall()
        # Totals are bounded so teams with many currencies stay under Telegram limits.
        lines.append('合计：' + (' / '.join(amount_text(Decimal(r['amount']) / (10 ** r['decimals']), r['currency'], conf) for r in sums[:8]) or '暂无'))
    rows = []
    for row in ids:
        entry = entry_view(conn, row['id'])
        converted = reference(entry['amount'], entry['currency'], conf, rates)
        lines.append(f'\n#{entry["id"]} {entry["description"]}\n{original(entry,conf)} · {entry_status(entry)}\n'
                     f'{entry["payer_name"]} · {entry["date"]}' + ('\n' + converted if converted else ''))
        if entry['reimbursed_minor']:
            lines.append(payment_totals(entry, conf))
        rows.append([button(f'查看 #{entry["id"]} · {entry["description"][:24]}', f'e:{entry["id"]}')])
    if not ids:
        lines.append('\n这里还没有账目。')
    paging = []
    if page:
        paging.append(button('← 上一页', f'list:{scope}:{page-1}'))
    if (page + 1) * PAGE_SIZE < count:
        paging.append(button('下一页 →', f'list:{scope}:{page+1}'))
    if paging:
        rows.append(paging)
    rows.append(nav())
    say(conn, chat_id, '\n'.join(lines), rows, edit=True)


def detail(conn, chat_id, entry_id, rates=None):
    conf, entry = settings(conn), entry_view(conn, entry_id)
    lines = [f'🧾 #{entry_id} · {entry_status(entry)}', entry['description'], original(entry, conf)]
    converted = reference(entry['amount'], entry['currency'], conf, rates)
    if converted:
        lines.append(converted)
    if entry['kind'] == 'expense':
        lines.append(payment_totals(entry, conf))
        remaining_reference = reference(entry['remaining_amount'], entry['currency'], conf, rates)
        if remaining_reference:
            lines.append('剩余待报 ' + remaining_reference)
    lines += [f'成员：{entry["payer_name"]}', f'日期：{entry["date"]}', f'备注：{entry["note"] or "未填写"}',
              f'凭证：{"已上传，Web 可查看" if entry["receipt"] else "未上传"}']
    if entry['review_note']:
        lines.append('处理备注：' + entry['review_note'])
    if entry['payments']:
        lines.append('\n最近付款（完整历史见 Web）：')
        for payment in entry['payments'][-3:]:
            lines.append(f'{payment["created_at"][:16]} · {amount_text(payment["amount"],payment["currency"],conf)} · {payment["name"]}\n'
                         f'抵扣 {amount_text(payment["entry_amount"],entry["currency"],conf)} · {payment["note"][:60]}')
    rows = []
    if entry['kind'] == 'expense' and entry['status'] in ('pending', 'draft') and not entry['reimbursed_minor']:
        rows.append([button('✏️ 修改账目', f'edit:{entry_id}')])
    if entry['kind'] == 'income' and entry['status'] == 'posted':
        rows.append([button('标记已分红到个人账户', f'distribute:{entry_id}')])
    if entry['status'] == 'distributed':
        lines.append('已分红到个人账户，不计团队可用资金、报销预算或结余。')
    if entry['status'] == 'pending':
        rows.append([button('💳 继续报销' if entry['reimbursed_minor'] else '💳 登记已付款', f'pay:{entry_id}')])
    if entry['status'] == 'draft':
        rows.append([button('记入账本', f'post:{entry_id}')])
    if entry['status'] not in ('reimbursed', 'voided') and not entry['reimbursed_minor']:
        rows.append([button('作废此账目', f'void:{entry_id}')])
    rows += [[button('📋 最近账目', 'list:recent:0')], nav()]
    say(conn, chat_id, '\n'.join(lines), rows, edit=True)


def period_summary(conn, chat_id, period):
    if period not in ('today', 'month'):
        fail('时间范围无效')
    conf, today = settings(conn), config.today()
    start = today if period == 'today' else today[:7] + '-01'
    rows = conn.execute("SELECT kind,currency,decimals,SUM(amount_minor) amount,COUNT(*) count FROM entries WHERE date>=? AND date<=? AND status NOT IN ('draft','voided','distributed') GROUP BY kind,currency,decimals", (start, today)).fetchall()
    lines = [f'📅 {"今日" if period == "today" else "本月"}收支 · {start} 至 {today}', '按发生日期统计，原币种分别列示；收入仅计团队在账，已分红收入不计。']
    for kind, label in [('income', '收入'), ('expense', '成员垫付')]:
        selected = [r for r in rows if r['kind'] == kind]
        lines.append(f'\n{label} · {sum(r["count"] for r in selected)} 笔\n' +
                     ('\n'.join(amount_text(Decimal(r['amount']) / (10 ** r['decimals']), r['currency'], conf) for r in selected) or '暂无'))
    say(conn, chat_id, '\n'.join(lines), [[button('今日', 'period:today'), button('本月', 'period:month')], nav()], edit=True)


def command_text(text, conf):
    if not text.startswith('/'):
        return '', text
    first, *rest = text.split(maxsplit=1)
    parts = first.split('@', 1)
    if len(parts) == 2 and parts[1].lower() != str(conf.get('bot_username', '')).lower():
        return 'foreign', ''
    return parts[0].lower(), rest[0] if rest else ''


def accepts_text(conn, msg, telegram_id, chat_id, draft):
    if msg.get('chat', {}).get('type') == 'private':
        return True
    reply_id = msg.get('reply_to_message', {}).get('message_id')
    if not reply_id or not draft:
        return False
    return bool(conn.execute('SELECT 1 FROM tg_prompts WHERE chat_id=? AND message_id=? AND telegram_id=? AND token=? AND step=? AND expires>?',
                             (chat_id, reply_id, telegram_id, draft['token'], draft['step'], int(time.time()))).fetchone())


def entry_snapshot(conn, entry_id):
    entry = dict(conn.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone())
    event_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE entry_id=?', (entry_id,)).fetchone()[0]
    return hashlib.sha256(json.dumps([entry, event_id], sort_keys=True).encode()).hexdigest()


def show_field(conn, chat_id, draft, field):
    data, token, conf = json.loads(draft['payload']), draft['token'], settings(conn)
    if field == 'currency':
        persist(conn, token, data, 'edit_currency')
        show_currencies(conn, chat_id, token, conf)
    elif field == 'payer':
        persist(conn, token, data, 'edit_payer')
        members = conn.execute('SELECT id,name FROM users WHERE active=1 ORDER BY id').fetchall()
        say(conn, chat_id, '选择垫付 / 经手成员：', [[button(m['name'], f'd:{token}:payer:{m["id"]}')] for m in members] + [[button('返回预览', f'd:{token}:back')]], edit=True)
    elif field == 'date':
        persist(conn, token, data, 'edit_date')
        say(conn, chat_id, '选择日期：', [[button('今天', f'd:{token}:day:0'), button('昨天', f'd:{token}:day:1')],
            [button('自定义日期', f'd:{token}:dateinput'), button('返回预览', f'd:{token}:back')]], edit=True)
    elif field in ('amount', 'description', 'note'):
        hints = {'amount': f'请输入金额（{data["currency"]}）。', 'description': '请输入用途 / 收入说明。', 'note': '请输入备注，发送 - 清空。'}
        ask(conn, chat_id, draft, 'edit_' + field, hints[field])
    else:
        fail('不支持修改该字段')


def draft_callback(conn, user, telegram_id, chat_id, callback_data, rates):
    parts = callback_data.split(':')
    if len(parts) < 3 or parts[0] != 'd':
        fail('此按钮已过期，请返回主菜单。')
    draft = conn.execute('SELECT * FROM tg_drafts WHERE token=? AND telegram_id=? AND chat_id=? AND user_id=? AND expires>?',
                         (parts[1], telegram_id, chat_id, user['id'], int(time.time()))).fetchone()
    if not draft:
        fail('这不是你当前的草稿，或它已确认 / 过期，请重新开始。')
    payload, step, token, conf = json.loads(draft['payload']), draft['step'], draft['token'], settings(conn)
    action = parts[2]
    if action == 'cancel':
        conn.execute('DELETE FROM tg_drafts WHERE token=?', (token,))
        say(conn, chat_id, '已关闭报销结果。' if payload.get('_receipt') else '已取消，此次填写没有入账。', [nav()], edit=True)
    elif action == 'incomestate' and step == 'confirm' and payload.get('kind') == 'income':
        payload['status'] = 'posted' if payload.get('status') == 'distributed' else 'distributed'
        persist(conn, token, payload, 'confirm')
        preview(conn, chat_id, token, payload, rates)
    elif action == 'edit' and len(parts) == 4 and step == 'confirm':
        show_field(conn, chat_id, draft, parts[3])
    elif action == 'resume':
        if payload.get('_flow') == 'funds':
            from . import bot_funds
            bot_funds.resume(conn, chat_id, draft, rates)
        elif step == 'confirm':
            preview(conn, chat_id, token, payload, rates)
        elif step == 'action_confirm':
            if payload.get('_action') == 'pay' and not payload.get('_quote'):
                show_payment_currencies(conn, chat_id, token, payload)
            else:
                action_preview(conn, chat_id, token, payload)
        elif step == 'pay_currency':
            show_payment_currencies(conn, chat_id, token, payload)
        elif step in ('currency', 'edit_currency'):
            show_currencies(conn, chat_id, token, conf)
        elif step in ('edit_date', 'edit_payer'):
            show_field(conn, chat_id, draft, step[5:])
        else:
            hints = {'amount': '请输入金额。', 'edit_amount': '请输入金额。',
                     'description': '请输入用途 / 收入说明。', 'edit_description': '请输入用途 / 收入说明。',
                     'edit_note': '请输入备注，发送 - 清空。', 'pay_note': '请输入付款方式 / 付款参考号。',
                     'pay_amount': f'请输入本次实际已报销金额（{payload.get("_quote",{}).get("currency","")}），发送 全额 报销剩余全部。',
                     'void_note': '请输入作废原因。', 'distribute_note': '请输入分红说明。', 'ai_text': '请描述一笔收支。'}
            ask(conn, chat_id, draft, step, hints.get(step, '请继续填写。'))
    elif action == 'paycur' and len(parts) == 4 and step == 'pay_currency':
        payload['_quote'] = make_payment_quote(conn, user, payload['_entry_id'], parts[3], rates)
        persist(conn, token, payload, 'pay_amount')
        quote = payload['_quote']
        ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), 'pay_amount',
            f'请填写本次实际已报销金额（{quote["currency"]}）。\n剩余全额：{quote["maximum_amount"]} {quote["currency"]}。\n'
            + (f'1 {quote["entry_currency"]} = {quote["rate"]} {quote["currency"]} · {quote["rate_date"]}\n' if quote['currency'] != quote['entry_currency'] else '')
            + '可以只报一部分；发送 全额 报销剩余全部。换算确认有效 30 分钟，请先完成真实转账。')
    elif action == 'paychange' and payload.get('_action') == 'pay':
        show_payment_currencies(conn, chat_id, token, payload)
    elif action == 'back' and step.startswith('edit_'):
        persist(conn, token, payload, 'confirm')
        preview(conn, chat_id, token, payload, rates)
    elif action == 'cur' and len(parts) == 4 and step in ('currency', 'edit_currency'):
        currency(conf, parts[3])
        payload['currency'] = parts[3]
        next_step = 'edit_amount' if step == 'edit_currency' else 'amount'
        persist(conn, token, payload, next_step)
        ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), next_step,
            f'币种 {parts[3]}，请{"重新" if step == "edit_currency" else ""}填写金额。')
    elif action == 'payer' and len(parts) == 4 and step == 'edit_payer' and parts[3].isdigit():
        payload['payer_id'] = int(parts[3])
        normalized_entry(conn, user, payload)
        persist(conn, token, payload, 'confirm')
        preview(conn, chat_id, token, payload, rates)
    elif action == 'day' and len(parts) == 4 and step == 'edit_date' and parts[3] in ('0', '1'):
        payload['date'] = (date.fromisoformat(config.today()) - timedelta(days=int(parts[3]))).isoformat()
        persist(conn, token, payload, 'confirm')
        preview(conn, chat_id, token, payload, rates)
    elif action == 'dateinput' and step == 'edit_date':
        ask(conn, chat_id, draft, 'edit_date', '请输入日期 YYYY-MM-DD。')
    elif action == 'save' and step == 'confirm':
        if payload.get('_edit_id'):
            if entry_snapshot(conn, payload['_edit_id']) != payload['_snapshot']:
                fail('账目已更新，请重新打开详情后修改。')
            entry = edit_entry(conn, user, payload['_edit_id'], payload)
        else:
            entry = add_entry(conn, user, payload, 'telegram', f'tg:draft:{token}')
        conn.execute('DELETE FROM tg_drafts WHERE token=?', (token,))
        say(conn, chat_id, f'✅ 已{"保存修改" if payload.get("_edit_id") else "入账"} #{entry["id"]}\n{entry["description"]}\n{original(entry,conf)}\n' +
            ('已加入待报销，付款后可直接标记已报销。' if entry['status'] == 'pending' else STATUS[entry['status']]),
            [[button('查看详情', f'e:{entry["id"]}'), button('我的待报销', 'summary')], nav()], edit=True)
    elif action == 'commit' and step == 'action_confirm':
        entry_id = payload['_entry_id']
        if entry_snapshot(conn, entry_id) != payload['_snapshot']:
            fail('账目已被其他操作更新，请重新打开详情核对。')
        if payload['_action'] == 'pay' and not payload.get('_quote'):
            show_payment_currencies(conn, chat_id, token, payload)
            return
        transition(conn, user, entry_id, payload['_action'], payload['note'],
                   **({'amount': payload['_pay_amount'], 'payment_key': f'pay:tg:{token}', 'quote_id': payload['_quote']['id']} if payload['_action'] == 'pay' else {}))
        conn.execute('DELETE FROM tg_drafts WHERE token=?', (token,))
        detail(conn, chat_id, entry_id, rates)
    else:
        fail('此按钮不属于当前步骤，请继续填写或用 /cancel 重新开始。')


def show_payment_currencies(conn, chat_id, token, payload):
    entry, conf = entry_view(conn, payload['_entry_id']), settings(conn)
    payload.pop('_quote', None)
    payload.pop('_pay_amount', None)
    persist(conn, token, payload, 'pay_currency')
    rows = [[button('按记账货币 · ' + entry['currency'], f'd:{token}:paycur:{entry["currency"]}')]]
    if conf['reference_currency'] != entry['currency']:
        rows.append([button('按结算货币 · ' + conf['reference_currency'], f'd:{token}:paycur:{conf["reference_currency"]}')])
    rows.append([button('取消', f'd:{token}:cancel')])
    say(conn, chat_id, f'💳 #{entry["id"]} {entry["description"]}\n{payment_totals(entry,conf)}\n\n选择本次实际付款的币种，再填写已报销金额。', rows, edit=True)


def action_preview(conn, chat_id, token, payload):
    entry, conf = entry_view(conn, payload['_entry_id']), settings(conn)
    action = payload['_action']
    label = {'pay': '登记已付款', 'void': '作废账目', 'submit': '记入账本', 'distribute': '标记已分红'}[action]
    description = {'pay': '请确认上述本次金额已经实际转账。系统只登记本次付款，不会发起转账。',
                   'distribute': '请确认整笔收入已分红到个人账户。确认后保留记录，并从团队资金和报销预算中排除，不会发起转账。',
                   'void': '账目保留历史，但不再计入有效收支。', 'submit': '草稿将进入待报销，金额不会从团队余额扣除。'}[action]
    payment_text = ''
    rows = [[button('确认' + label, f'd:{token}:commit')]]
    if action == 'pay':
        user = {'id': conn.execute('SELECT user_id FROM tg_drafts WHERE token=?', (token,)).fetchone()[0]}
        _, quote, units, allocation = payment_preview(conn, user, entry['id'], payload['_pay_amount'], payload['_quote']['id'])
        paid = entry['reimbursed_minor'] + allocation
        payment_text = f'\n本次实际付款：{amount_text(Decimal(units)/(10**quote["decimals"]),quote["currency"],conf)}\n'
        payment_text += f'抵扣记账金额：{amount_text(Decimal(allocation)/(10**entry["decimals"]),entry["currency"],conf)}\n'
        payment_text += f'登记后累计已报：{amount_text(Decimal(paid)/(10**entry["decimals"]),entry["currency"],conf)}\n剩余待报：{amount_text(Decimal(entry["amount_minor"]-paid)/(10**entry["decimals"]),entry["currency"],conf)}\n'
        if quote['currency'] != entry['currency']:
            payment_text += f'汇率：1 {entry["currency"]} = {quote["rate"]} {quote["currency"]} · {quote["rate_date"]}\n'
        rows.append([button('修改币种 / 金额', f'd:{token}:paychange')])
    say(conn, chat_id, f'⚠️ 确认{label}\n#{entry["id"]} {entry["description"]}\n原金额 {original(entry,conf)}\n' + payment_text +
        f'成员：{entry["payer_name"]}\n备注：{payload["note"]}\n\n' +
        description,
        rows + [[button('取消', f'd:{token}:cancel')]], edit=True)


def handle(conn, update, ai_result=None, ai_error=None, rates=None):
    conf = settings(conn)
    cb = update.get('callback_query')
    msg = cb.get('message', {}) if cb else update.get('message', {})
    sender = cb.get('from', {}) if cb else msg.get('from', {})
    telegram_id, chat_id = str(sender.get('id', '')), str(msg.get('chat', {}).get('id', ''))
    private = msg.get('chat', {}).get('type') == 'private'
    if not telegram_id or not chat_id or sender.get('is_bot'):
        return
    if cb:
        queue(conn, 'answerCallbackQuery', {'callback_query_id': cb['id']})
    if not private and chat_id not in conf['telegram_allowed_chats']:
        return
    text = str(msg.get('text', '')).strip()[:2500] if not cb else ''
    command, argument = command_text(text, conf)
    if command == 'foreign':
        return
    if command == '/start' and argument:
        if not private:
            say(conn, chat_id, '绑定码请只发送给机器人私聊。')
            return
        row = conn.execute('SELECT l.*,u.active FROM link_codes l JOIN users u ON u.id=l.user_id WHERE code_hash=? AND expires>?',
                           (hashlib.sha256(argument.encode()).hexdigest(), int(time.time()))).fetchone()
        if not row or not row['active']:
            say(conn, chat_id, '绑定码无效或已过期，请到 Web「机器人与 AI」重新生成。')
            return
        conn.execute('DELETE FROM tg_drafts WHERE user_id=? OR telegram_id=?', (row['user_id'], telegram_id))
        conn.execute('DELETE FROM tg_links WHERE user_id=? OR telegram_id=?', (row['user_id'], telegram_id))
        conn.execute('INSERT INTO tg_links VALUES (?,?)', (telegram_id, row['user_id']))
        conn.execute('DELETE FROM link_codes WHERE user_id=?', (row['user_id'],))
    linked = conn.execute('SELECT u.* FROM tg_links t JOIN users u ON u.id=t.user_id WHERE t.telegram_id=? AND u.active=1', (telegram_id,)).fetchone()
    if not linked:
        if private or cb or command:
            say(conn, chat_id, '请先登录 Web，在「机器人与 AI」生成绑定码，私聊发送 /start 绑定码。', [[web_button()]] if web_button() else None)
        return
    user, data = dict(linked), str(cb.get('data', '')) if cb else ''
    conn.execute('DELETE FROM tg_drafts WHERE expires<?', (int(time.time()),))
    conn.execute('DELETE FROM tg_prompts WHERE expires<?', (int(time.time()),))
    if command in ('/start', '/menu') or data == 'home':
        menu(conn, user, chat_id)
    elif command == '/help' or data == 'help':
        help_menu(conn, chat_id)
    elif command == '/cancel' or data == 'cancel':
        conn.execute('DELETE FROM tg_drafts WHERE telegram_id=? AND chat_id=?', (telegram_id, chat_id))
        say(conn, chat_id, '已取消当前填写。', [nav()], edit=True)
    elif command in ('/recent', '/summary', '/team') or data == 'summary' or data.startswith('list:'):
        scope, page = {'/recent': 'recent', '/summary': 'mine', '/team': 'team'}.get(command, 'mine'), 0
        if data.startswith('list:'):
            parts = data.split(':')
            if len(parts) != 3 or not parts[2].isdigit():
                fail('分页参数无效')
            scope, page = parts[1], int(parts[2])
        list_entries(conn, user, chat_id, scope, page, rates)
    elif command in ('/today', '/month') or data.startswith('period:'):
        period_summary(conn, chat_id, command[1:] if command else data.split(':', 1)[1])
    elif command == '/funds' or data == 'funds':
        from . import bot_funds
        bot_funds.start(conn, user, telegram_id, chat_id, rates)
    elif command in ('/book', '/income') or data in ('new', 'newincome'):
        token, payload = new_draft(conn, user, telegram_id, chat_id)
        if command == '/income' or data == 'newincome':
            payload.update(kind='income', status='posted')
            persist(conn, token, payload, 'currency')
        show_currencies(conn, chat_id, token, conf)
    elif data == 'ai' or command == '/ai' and not argument:
        new_draft(conn, user, telegram_id, chat_id, step='ai_text')
        ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), 'ai_text', '请描述一笔收支，例如「昨天打车花了35元」。')
    elif data.startswith(('e:', 'edit:', 'pay:', 'void:', 'post:', 'distribute:')):
        entry_action(conn, user, telegram_id, chat_id, data, rates)
    elif data.startswith('f:'):
        from . import bot_funds
        bot_funds.callback(conn, user, telegram_id, chat_id, data, rates)
    elif cb:
        draft_callback(conn, user, telegram_id, chat_id, data, rates)
    elif command and command != '/ai':
        help_menu(conn, chat_id)
    else:
        text_input(conn, user, msg, telegram_id, chat_id, text, command, ai_result, ai_error, rates)


def entry_action(conn, user, telegram_id, chat_id, data, rates):
    action, value = data.split(':', 1)
    if not value.isdigit():
        fail('账目编号无效')
    entry = entry_view(conn, int(value))
    if action == 'e':
        detail(conn, chat_id, entry['id'], rates)
        return
    if action == 'edit':
        if entry['kind'] != 'expense' or entry['status'] not in ('draft', 'pending') or entry['reimbursed_minor']:
            fail('只可修改草稿或待报销支出')
        body = {k: entry[k] for k in ('kind', 'amount', 'currency', 'description', 'date', 'note', 'payer_id', 'status')}
        body.update(_edit_id=entry['id'], _snapshot=entry_snapshot(conn, entry['id']))
        token, body = new_draft(conn, user, telegram_id, chat_id, body)
        preview(conn, chat_id, token, body, rates)
        return
    if action == 'pay' and entry['status'] != 'pending':
        fail('只有待报销账目可以登记付款')
    if action == 'void' and (entry['status'] in ('reimbursed', 'voided') or entry['reimbursed_minor']):
        fail('已报销或已作废账目不能作废')
    if action == 'post':
        if entry['kind'] != 'expense' or entry['status'] != 'draft':
            fail('仅草稿可以记入账本')
        body = {'_action': 'submit', '_entry_id': entry['id'], '_snapshot': entry_snapshot(conn, entry['id']), 'note': ''}
        token, body = new_draft(conn, user, telegram_id, chat_id, body, step='action_confirm')
        action_preview(conn, chat_id, token, body)
        return
    if action == 'distribute' and (entry['kind'] != 'income' or entry['status'] != 'posted'):
        fail('只有团队在账收入可以标记已分红')
    body = {'_action': action, '_entry_id': entry['id'], '_snapshot': entry_snapshot(conn, entry['id'])}
    if action == 'pay':
        token, body = new_draft(conn, user, telegram_id, chat_id, body, step='pay_currency')
        show_payment_currencies(conn, chat_id, token, body)
        return
    new_draft(conn, user, telegram_id, chat_id, body, step=action + '_note')
    ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), action + '_note',
        f'处理 #{entry["id"]}「{entry["description"]}」\n' + ('请填写付款方式 / 付款参考号，请先完成真实转账。' if action == 'pay' else '请填写分红说明，下一步确认后从团队余额排除。' if action == 'distribute' else '请填写作废原因。'))


def text_input(conn, user, msg, telegram_id, chat_id, text, command, ai_result, ai_error, rates):
    draft, conf = draft_for(conn, telegram_id, chat_id), settings(conn)
    if draft and command != '/ai' and accepts_text(conn, msg, telegram_id, chat_id, draft):
        step, payload = draft['step'], json.loads(draft['payload'])
        if payload.get('_flow') == 'funds':
            from . import bot_funds
            bot_funds.text_input(conn, chat_id, draft, text, rates)
            return
        if step == 'ai_text':
            pass
        elif step == 'pay_amount':
            amount = payload['_quote']['maximum_amount'] if text == '全额' else text
            payment_preview(conn, user, payload['_entry_id'], amount, payload['_quote']['id'])
            payload['_pay_amount'] = amount
            persist(conn, draft['token'], payload, 'pay_note')
            ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), 'pay_note', '请输入付款方式 / 付款参考号。下一步会展示金额与剩余款，确认后才登记。')
            return
        elif step in ('pay_note', 'void_note', 'distribute_note'):
            payload['note'] = text_value(text, '付款备注 / 作废原因', 500)
            if step == 'pay_note' and not payload.get('_quote'):
                show_payment_currencies(conn, chat_id, draft['token'], payload)
                return
            persist(conn, draft['token'], payload, 'action_confirm')
            action_preview(conn, chat_id, draft['token'], payload)
            return
        elif step in ('amount', 'description', 'edit_amount', 'edit_description', 'edit_note', 'edit_date'):
            if step in ('amount', 'edit_amount'):
                minor_amount(text, currency(conf, payload['currency'])['decimals'])
                payload['amount'] = text
            elif step in ('description', 'edit_description'):
                payload['description'] = text_value(text, '用途', 120)
            elif step == 'edit_note':
                payload['note'] = '' if text == '-' else text_value(text, '备注', 1000, False)
            else:
                try:
                    payload['date'] = date.fromisoformat(text).isoformat()
                except ValueError:
                    fail('日期请使用 YYYY-MM-DD 格式。')
            if step == 'amount':
                persist(conn, draft['token'], payload, 'description')
                ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), 'description', '3/3 · 请输入用途 / 收入说明。')
            else:
                normalized_entry(conn, user, payload)
                persist(conn, draft['token'], payload, 'confirm')
                preview(conn, chat_id, draft['token'], payload, rates)
            return
        else:
            say(conn, chat_id, '请点击当前草稿的按钮继续，或发送 /cancel。', [nav()])
            return
    if ai_error:
        say(conn, chat_id, ai_error, [[button('改用按钮记账', 'new')], nav()])
    elif ai_result:
        normalized_entry(conn, user, ai_result)
        token, payload = new_draft(conn, user, telegram_id, chat_id, ai_result)
        preview(conn, chat_id, token, payload, rates)
    elif msg.get('chat', {}).get('type') == 'private' and text:
        say(conn, chat_id, '请使用菜单继续，或发送 /help 查看用法。', [nav()])
