"""Telegram long-polling worker. One worker per ledger database."""
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from decimal import Decimal
from fastapi import HTTPException
from . import ai, config
from .db import db, init_db, settings, set_setting
from .domain import add_entry, admin, currency, minor_amount, normalized_entry, text_value


def queue(conn, method, payload):
    conn.execute('INSERT INTO tg_outbox(method,payload) VALUES (?,?)', (method, json.dumps(payload, ensure_ascii=False)))


def say(conn, chat_id, text, buttons=None):
    payload = {'chat_id': chat_id, 'text': text}
    if buttons:
        payload['reply_markup'] = {'inline_keyboard': buttons}
    queue(conn, 'sendMessage', payload)


def button(text, data):
    return {'text': text, 'callback_data': data}


def menu(conn, chat_id, name):
    say(conn, chat_id, f'{name}，今天也记清每一笔。\n\n按钮逐步填写，或直接说：今天打车花了 35 元。\nAI 识别后需要你确认才入账。',
        [[button('＋ 成员垫付', 'new'), button('＋ 团队收入', 'newincome')], [button('查看我的待报销', 'summary'), button('取消当前填写', 'cancel')]])


def new_draft(conn, user, telegram_id, chat_id, payload=None):
    conn.execute('DELETE FROM tg_drafts WHERE telegram_id=? AND chat_id=?', (telegram_id, chat_id))
    token = secrets.token_hex(5)
    step = 'confirm' if payload else 'currency'
    payload = payload or {'kind': 'expense', 'date': config.today(), 'note': '', 'payer_id': user['id'], 'status': 'submitted'}
    conn.execute('INSERT INTO tg_drafts VALUES (?,?,?,?,?,?,?)',
                 (token, telegram_id, chat_id, user['id'], step, json.dumps(payload, ensure_ascii=False), int(time.time()) + 3600))
    return token, payload


def preview(conn, chat_id, token, data):
    kind = '收入' if data['kind'] == 'income' else '支出 · 提交报销'
    say(conn, chat_id, f'请确认这笔{kind}\n\n用途：{data["description"]}\n金额：{data["amount"]} {data["currency"]}\n日期：{data["date"]}\n\n确认后会同步到 Web 账本。',
        [[button('确认入账', f'd:{token}:save'), button('取消', f'd:{token}:cancel')]])


def show_currencies(conn, chat_id, token, conf):
    buttons = [button(c['code'] + ' ' + c['name'], f'd:{token}:cur:{c["code"]}') for c in conf['currencies']]
    say(conn, chat_id, '先选择这笔账目的币种：', [buttons[i:i+2] for i in range(0, len(buttons), 2)])


def summary(conn, user, chat_id):
    rows = conn.execute("SELECT currency,decimals,SUM(amount_minor) amount FROM entries WHERE payer_id=? AND status IN ('submitted','approved') GROUP BY currency,decimals", (user['id'],)).fetchall()
    lines = [f'{r["currency"]} {Decimal(r["amount"]) / (10 ** r["decimals"]):.{r["decimals"]}f}' for r in rows]
    say(conn, chat_id, '你的待报销金额（含待审核）：\n' + ('\n'.join(lines) if lines else '暂时没有，账目很清爽。'))


def handle(conn, update, ai_result=None, ai_error=None):
    conf = settings(conn)
    callback = update.get('callback_query')
    msg = callback.get('message', {}) if callback else update.get('message', {})
    sender = callback.get('from', {}) if callback else msg.get('from', {})
    telegram_id = str(sender.get('id', ''))
    chat_id = str(msg.get('chat', {}).get('id', ''))
    private = msg.get('chat', {}).get('type') == 'private'
    if not telegram_id or not chat_id or sender.get('is_bot'):
        return
    if callback:
        queue(conn, 'answerCallbackQuery', {'callback_query_id': callback['id']})
    if not private and chat_id not in conf['telegram_allowed_chats']:
        # Do not post to arbitrary groups the bot has been added to.
        return
    text = str(msg.get('text', '')).strip()[:2500] if not callback else ''
    command = text.split(maxsplit=1)[0].split('@')[0] if text.startswith('/') else ''
    if command == '/start' and len(text.split()) == 2:
        if not private:
            say(conn, chat_id, '账号绑定请在机器人的私聊中完成，不要把绑定码发到群里。')
            return
        code = text.split()[1]
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        row = conn.execute('SELECT l.*,u.active FROM link_codes l JOIN users u ON u.id=l.user_id WHERE code_hash=? AND expires>?', (code_hash, int(time.time()))).fetchone()
        if not row or not row['active']:
            say(conn, chat_id, '绑定码无效或已过期，请在 Web 的「机器人」页面重新生成。')
            return
        # Rebinding revokes the old Telegram account and invalidates its outstanding drafts.
        conn.execute('DELETE FROM tg_drafts WHERE user_id=? OR telegram_id=?', (row['user_id'], telegram_id))
        conn.execute('DELETE FROM tg_links WHERE user_id=? OR telegram_id=?', (row['user_id'], telegram_id))
        conn.execute('INSERT INTO tg_links VALUES (?,?)', (telegram_id, row['user_id']))
        conn.execute('DELETE FROM link_codes WHERE user_id=?', (row['user_id'],))
        say(conn, chat_id, '绑定成功。Web 和 Telegram 现在共用你的团队账号。')
    linked = conn.execute('SELECT u.* FROM tg_links t JOIN users u ON u.id=t.user_id WHERE t.telegram_id=? AND u.active=1', (telegram_id,)).fetchone()
    if not linked:
        say(conn, chat_id, '请先登录团队 Web 账本，在「机器人」页面生成一次性绑定码，然后私聊发送 /start 绑定码。')
        return
    user = dict(linked)
    data = str(callback.get('data', '')) if callback else ''
    if command in ('/start', '/help'):
        menu(conn, chat_id, user['name'])
        return
    if command == '/cancel' or data == 'cancel':
        conn.execute('DELETE FROM tg_drafts WHERE telegram_id=? AND chat_id=?', (telegram_id, chat_id))
        say(conn, chat_id, '已取消当前填写。发送 /book 可以重新记账。')
        return
    if command == '/summary' or data == 'summary':
        summary(conn, user, chat_id)
        return
    if command in ('/book', '/income') or data in ('new', 'newincome'):
        token, payload = new_draft(conn, user, telegram_id, chat_id)
        if command == '/income' or data == 'newincome':
            payload.update(kind='income', status='posted')
            conn.execute('UPDATE tg_drafts SET payload=? WHERE token=?', (json.dumps(payload, ensure_ascii=False), token))
        show_currencies(conn, chat_id, token, conf)
        return
    conn.execute('DELETE FROM tg_drafts WHERE expires<?', (int(time.time()),))
    if callback:
        parts = data.split(':')
        if len(parts) < 3 or parts[0] != 'd':
            return
        draft = conn.execute('SELECT * FROM tg_drafts WHERE token=? AND telegram_id=? AND chat_id=? AND user_id=?', (parts[1], telegram_id, chat_id, user['id'])).fetchone()
        if not draft:
            say(conn, chat_id, '这不是你当前的草稿，或它已经确认 / 过期。请发送 /book 重新开始。')
            return
        payload = json.loads(draft['payload'])
        step = draft['step']
        if parts[2] == 'cancel':
            conn.execute('DELETE FROM tg_drafts WHERE token=?', (draft['token'],))
            say(conn, chat_id, '已取消，没有入账。')
            return
        if parts[2] == 'cur' and len(parts) == 4 and step == 'currency':
            currency(conf, parts[3])
            payload['currency'] = parts[3]
            step = 'amount'
            say(conn, chat_id, f'请输入金额（{payload["currency"]}），例如 35.50。')
        elif parts[2] == 'save' and step == 'confirm':
            # Authorization, currency precision and all fields are validated again at confirmation.
            entry = add_entry(conn, user, payload, 'telegram', f'tg:draft:{draft["token"]}')
            conn.execute('DELETE FROM tg_drafts WHERE token=?', (draft['token'],))
            say(conn, chat_id, f'已入账 #{entry["id"]}\n{entry["description"]} · {entry["amount"]} {entry["currency"]}\n' + ('已提交报销，等待审核。' if entry['kind'] == 'expense' else '团队收入已记录。'), [[button('再记一笔', 'new'), button('我的待报销', 'summary')]])
            return
        else:
            say(conn, chat_id, '请按当前步骤填写，或用 /cancel 重新开始。')
            return
        conn.execute('UPDATE tg_drafts SET step=?,payload=? WHERE token=?', (step, json.dumps(payload, ensure_ascii=False), draft['token']))
        return
    draft = conn.execute('SELECT * FROM tg_drafts WHERE telegram_id=? AND chat_id=?', (telegram_id, chat_id)).fetchone()
    if draft and command != '/ai':
        payload = json.loads(draft['payload'])
        if draft['step'] == 'amount':
            cur = currency(conf, payload['currency'])
            minor_amount(text, cur['decimals'])
            payload['amount'] = text
            step = 'description'
            say(conn, chat_id, '请输入收入说明，例如：客户项目尾款。' if payload['kind'] == 'income' else '这笔钱花在了哪里？请输入用途，例如：周五团队午餐。')
        elif draft['step'] == 'description':
            payload['description'] = text_value(text, '用途', 120)
            normalized_entry(conn, user, payload)
            step = 'confirm'
            preview(conn, chat_id, draft['token'], payload)
        else:
            say(conn, chat_id, '请点击上方按钮继续，或用 /cancel 取消。')
            return
        conn.execute('UPDATE tg_drafts SET step=?,payload=? WHERE token=?', (step, json.dumps(payload, ensure_ascii=False), draft['token']))
        return
    if ai_error:
        say(conn, chat_id, ai_error)
    elif ai_result:
        normalized_entry(conn, user, ai_result)
        token, payload = new_draft(conn, user, telegram_id, chat_id, ai_result)
        preview(conn, chat_id, token, payload)
    elif text:
        say(conn, chat_id, 'AI 尚未配置或内容无法识别。按钮记账无需 AI，发送 /book 即可开始。')


def process_update(update):
    update_id = update.get('update_id')
    if type(update_id) is not int:
        return
    with db() as conn:
        if conn.execute('SELECT 1 FROM tg_updates WHERE update_id=?', (update_id,)).fetchone():
            return
        conf = settings(conn)
        msg = update.get('message', {})
        text = str(msg.get('text', '')).strip()[:2500]
        telegram_id = str(msg.get('from', {}).get('id', ''))
        chat_id = str(msg.get('chat', {}).get('id', ''))
        authorized_chat = msg.get('chat', {}).get('type') == 'private' or chat_id in conf['telegram_allowed_chats']
        linked = conn.execute('SELECT u.id FROM tg_links t JOIN users u ON u.id=t.user_id WHERE t.telegram_id=? AND u.active=1', (telegram_id,)).fetchone()
        draft = conn.execute('SELECT 1 FROM tg_drafts WHERE telegram_id=? AND chat_id=? AND expires>?', (telegram_id, chat_id, int(time.time()))).fetchone()
    result = None
    error = None
    command = text.split(maxsplit=1)[0].split('@')[0] if text.startswith('/') else ''
    if linked and authorized_chat and text and (command == '/ai' or (not command and not draft)):
        try:
            result = ai.parse(text.split(maxsplit=1)[1] if command == '/ai' and ' ' in text else text, conf)
        except HTTPException as exc:
            error = exc.detail
    with db(write=True) as conn:
        if conn.execute('SELECT 1 FROM tg_updates WHERE update_id=?', (update_id,)).fetchone():
            return
        # A savepoint keeps update acknowledgement and error messages durable while
        # rolling back any partial business changes from a rejected interaction.
        conn.execute('SAVEPOINT handling')
        try:
            handle(conn, update, result, error)
        except HTTPException as exc:
            conn.execute('ROLLBACK TO handling')
            callback = update.get('callback_query')
            msg = callback.get('message', {}) if callback else update.get('message', {})
            if callback:
                queue(conn, 'answerCallbackQuery', {'callback_query_id': callback['id'], 'text': str(exc.detail)[:160]})
            chat_id = msg.get('chat', {}).get('id')
            if chat_id:
                say(conn, chat_id, str(exc.detail))
        conn.execute('RELEASE handling')
        conn.execute('INSERT INTO tg_updates VALUES (?,?)', (update_id, config.now()))
        set_setting(conn, 'bot_offset', update_id + 1)
        set_setting(conn, 'bot_heartbeat', int(time.time()))


def api(method, payload):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    proxy = os.getenv('TELEGRAM_PROXY_URL')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy}) if proxy else urllib.request.ProxyHandler())
    request = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=40) as response:
            body = json.loads(response.read(4_000_000))
        if not body.get('ok'):
            raise RuntimeError('Telegram API rejected the operation')
        return body.get('result')
    except urllib.error.HTTPError as exc:
        # URLs contain the bot token. Never print urllib exception strings.
        raise RuntimeError(f'Telegram HTTP {exc.code}') from None
    except Exception:
        raise RuntimeError('Telegram connection failed; verify connectivity and bot configuration') from None


def drain_outbox():
    with db() as conn:
        pending = [dict(r) for r in conn.execute('SELECT * FROM tg_outbox WHERE sent=0 AND attempts<8 ORDER BY id LIMIT 25')]
    for row in pending:
        try:
            api(row['method'], json.loads(row['payload']))
            with db(write=True) as conn:
                conn.execute('UPDATE tg_outbox SET sent=1 WHERE id=?', (row['id'],))
        except RuntimeError:
            with db(write=True) as conn:
                conn.execute('UPDATE tg_outbox SET attempts=attempts+1 WHERE id=?', (row['id'],))
            break


def main():
    init_db()
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        print('TELEGRAM_BOT_TOKEN 未配置。请填写 .env 后重新启动机器人。')
        return
    try:
        me = api('getMe', {})
        webhook = api('getWebhookInfo', {})
        if webhook.get('url'):
            print('机器人已配置 Webhook，无法同时长轮询。请先在原服务停用 Webhook，再启动本 worker。')
            return
        with db(write=True) as conn:
            set_setting(conn, 'bot_username', me.get('username'))
        print('Telegram 机器人已启动；按 Ctrl+C 停止。')
    except RuntimeError as exc:
        print(str(exc))
        return
    while True:
        try:
            drain_outbox()
            with db() as conn:
                offset = settings(conn).get('bot_offset', 0)
            updates = api('getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['message', 'callback_query']})
            for update in updates:
                process_update(update)
            with db(write=True) as conn:
                set_setting(conn, 'bot_heartbeat', int(time.time()))
            drain_outbox()
        except KeyboardInterrupt:
            break
        except Exception:
            print('机器人连接或处理失败，5 秒后重试；详情请检查配置及数据库。')
            time.sleep(5)


if __name__ == '__main__':
    main()
