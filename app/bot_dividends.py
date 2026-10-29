"""Telegram surplus dividends with owner-scoped previews and immutable history."""
import json
import time
from . import dividends
from .db import settings
from .domain import fail, text_value
from .bot_ui import amount_text, ask, button, draft_for, nav, new_draft, persist, say


def home(conn, user, chat_id, token, payload, rates):
    conf = settings(conn)
    code = payload.get('currency', conf['reference_currency'])
    payload.pop('_quote', None)
    persist(conn, token, payload, 'dividend_home')
    if not rates or rates.get('stale') or rates.get('missing'):
        say(conn, chat_id, '盈余分红暂缺有效汇率，请稍后刷新。分红记录仍可查看。',
            [[button('刷新盈余', 'dividends'), button('分红记录', f'v:{token}:history:0')], nav()], edit=True)
        return
    quoted = dividends.quote(conn, user, code, rates)
    payload['_quote'] = quoted
    persist(conn, token, payload, 'dividend_home')
    text = ('💰 团队资金分红\n\n可分红盈余：' + amount_text(quoted['surplus'], quoted['reference_currency'], conf) +
            '\n本次按 ' + code + ' 分红，最多：' + amount_text(quoted['maximum_amount'], code, conf) +
            '\n\n可分红 = 收入 − 已报销 − 已分红（最低为 0）。待报销未结清也可分红。\n模拟进账不参与分红。收入和报销记录正常保留；实际完成转账后再登记。')
    rows = [[button('填写本次已分红金额', f'v:{token}:amount'), button('全部可分红盈余', f'v:{token}:all')],
            [button('选择分红币种 · ' + code, f'v:{token}:currencies:0')],
            [button('分红记录', f'v:{token}:history:0'), button('刷新盈余', 'dividends')],
            [button('资金计算', 'funds')], nav()]
    say(conn, chat_id, text, rows, edit=True)


def start(conn, user, telegram_id, chat_id, rates):
    token, payload = new_draft(conn, user, telegram_id, chat_id, {'_flow': 'dividends'}, 'dividend_home')
    home(conn, user, chat_id, token, payload, rates)


def show_preview(conn, user, chat_id, token, payload):
    conf = settings(conn)
    value = dividends.preview(conn, user, payload['_quote']['id'], payload['_amount'])
    payload['_preview'] = value
    persist(conn, token, payload, 'dividend_confirm')
    text = ('💰 确认本次已完成的盈余分红\n\n本次实际分红：' + amount_text(value['amount'], value['currency'], conf) +
            '\n登记后可分红盈余参考：' + amount_text(value['remaining_surplus'], value['reference_currency'], conf) +
            '\n说明：' + payload.get('note', '盈余分红') + '\n\n待报销款不强制预留，分红会减少后续可报销资金。确认后只扣减本次分红，未结清的报销继续保留。系统不会执行转账。')
    if value['currency'] != value['reference_currency']:
        text += f'\n参考汇率：1 {value["currency"]} = {value["rate"]} {value["reference_currency"]} · {value["rate_date"]}'
    say(conn, chat_id, text,
        [[button('✅ 确认已转账，登记分红', f'v:{token}:commit')],
         [button('填写分红说明', f'v:{token}:note'), button('取消', f'v:{token}:cancel')]], edit=True)


def history(conn, chat_id, token, page):
    conf = settings(conn)
    items = dividends.history(conn)
    count = len(items)
    pages = max((count + 4) // 5, 1)
    page = min(max(page, 0), pages - 1)
    lines = [f'📜 盈余分红记录 · 第 {page+1}/{pages} 页 · 共 {count} 笔']
    for item in items[page*5:page*5+5]:
        lines.append(f'\nD{item["id"]} · ' + amount_text(item['amount'], item['currency'], conf) +
                     f'\n{item["name"][:40]} · {item["created_at"][:16]}\n{item["note"][:150]}')
    if not items:
        lines.append('暂无分红记录。')
    paging = []
    if page:
        paging.append(button('← 上一页', f'v:{token}:history:{page-1}'))
    if page + 1 < pages:
        paging.append(button('下一页 →', f'v:{token}:history:{page+1}'))
    say(conn, chat_id, '\n'.join(lines), ([paging] if paging else []) + [[button('返回盈余分红', 'dividends')], nav()], edit=True)


def currencies(conn, chat_id, token, page):
    choices = settings(conn)['currencies']
    pages = max((len(choices) + 4) // 5, 1)
    page = min(max(page, 0), pages-1)
    rows = [[button(c['code']+' · '+c['name'], f'v:{token}:cur:{c["code"]}')] for c in choices[page*5:page*5+5]]
    paging = []
    if page:
        paging.append(button('← 上一页', f'v:{token}:currencies:{page-1}'))
    if page+1 < pages:
        paging.append(button('下一页 →', f'v:{token}:currencies:{page+1}'))
    say(conn, chat_id, '选择本次实际分红的币种。\n系统将实际可用余额折算为此币种，确认前请核对实际转账金额。',
        rows + ([paging] if paging else []) + [[button('返回盈余分红', 'dividends')]], edit=True)


def receipt(conn, chat_id, token, value):
    conf = settings(conn)
    say(conn, chat_id, f'✅ 已登记盈余分红 D{value["id"]}\n' + amount_text(value['amount'], value['currency'], conf) +
        '\n' + value['note'] + '\n收入和报销记录已保留，团队余额已扣减本次分红。',
        [[button('查看最新盈余', 'dividends'), button('分红记录', f'v:{token}:history:0')], nav()], edit=True)


def callback(conn, user, telegram_id, chat_id, data, rates):
    parts = data.split(':')
    if len(parts) < 3:
        fail('分红按钮无效，请发送 /dividends 重新打开。')
    draft = conn.execute('SELECT * FROM tg_drafts WHERE token=? AND telegram_id=? AND chat_id=? AND user_id=? AND expires>?',
                         (parts[1], telegram_id, chat_id, user['id'], int(time.time()))).fetchone()
    if not draft:
        fail('这不是你的当前分红填写，或它已过期，请发送 /dividends 重新打开。')
    payload, token, step = json.loads(draft['payload']), draft['token'], draft['step']
    if payload.get('_flow') != 'dividends':
        fail('请发送 /dividends 重新打开。')
    action = parts[2]
    if action in ('history', 'currencies') and len(parts) == 4 and parts[3].isdigit() and len(parts[3]) < 7:
        if action == 'history':
            history(conn, chat_id, token, int(parts[3]))
        else:
            if step not in ('dividend_home', 'dividend_currency'):
                fail('当前步骤不能更改币种，请发送 /dividends 重新核对。')
            persist(conn, token, payload, 'dividend_currency')
            currencies(conn, chat_id, token, int(parts[3]))
    elif action == 'cur' and len(parts) == 4 and step == 'dividend_currency':
        payload['currency'] = parts[3]
        home(conn, user, chat_id, token, payload, rates)
    elif action in ('amount', 'all') and step == 'dividend_home' and payload.get('_quote'):
        dividends.preview(conn, user, payload['_quote']['id'], 'all')
        if action == 'all':
            payload['_amount'] = 'all'
            payload['note'] = '全部可分红盈余已转账'
            show_preview(conn, user, chat_id, token, payload)
        else:
            ask(conn, chat_id, draft, 'dividend_amount', f'请输入本次已分红金额（{payload["_quote"]["currency"]}），最多 {payload["_quote"]["maximum_amount"]}。这是本次金额，不是累计金额。')
    elif action == 'note' and step == 'dividend_confirm':
        ask(conn, chat_id, draft, 'dividend_note', '填写本次分红说明 / 转账参考号，下一步核对确认。')
    elif action == 'cancel' and step != 'dividend_receipt':
        conn.execute('DELETE FROM tg_drafts WHERE token=?', (token,))
        say(conn, chat_id, '已取消，本次分红没有登记。', [[button('返回盈余分红', 'dividends')], nav()], edit=True)
    elif action == 'commit' and step == 'dividend_receipt':
        receipt(conn, chat_id, token, payload['_receipt'])
    elif action == 'commit' and step == 'dividend_confirm':
        value = dividends.record(conn, user, payload['_quote']['id'], payload['_amount'], payload['note'], f'dividend:tg:{token}')
        payload['_receipt'] = value
        persist(conn, token, payload, 'dividend_receipt')
        receipt(conn, chat_id, token, value)
    else:
        fail('分红步骤已变化，请发送 /dividends 重新核对盈余。')


def text_input(conn, user, chat_id, draft, text):
    payload = json.loads(draft['payload'])
    if draft['step'] == 'dividend_amount':
        dividends.preview(conn, user, payload['_quote']['id'], text)
        payload['_amount'], payload['note'] = text, '盈余分红'
    elif draft['step'] == 'dividend_note':
        payload['note'] = text_value(text, '分红说明', 500)
    else:
        say(conn, chat_id, '请点击分红按钮继续，或发送 /dividends 重新核对。', [nav()])
        return
    show_preview(conn, user, chat_id, draft['token'], payload)


def resume(conn, user, chat_id, draft, rates):
    payload, token = json.loads(draft['payload']), draft['token']
    if draft['step'] == 'dividend_confirm':
        show_preview(conn, user, chat_id, token, payload)
    elif draft['step'] == 'dividend_receipt':
        receipt(conn, chat_id, token, payload['_receipt'])
    elif draft['step'] in ('dividend_amount', 'dividend_note'):
        ask(conn, chat_id, draft, draft['step'], '请输入本次分红金额。' if draft['step'] == 'dividend_amount' else '请输入分红说明。')
    else:
        home(conn, user, chat_id, token, payload, rates)
