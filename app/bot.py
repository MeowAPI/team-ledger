"""Telegram long-polling worker with durable replies. One worker per database."""
import json
import os
import time
import urllib.error
import urllib.request
from fastapi import HTTPException
from . import ai, config, fx
from .db import db, init_db, settings, set_setting
from .bot_ui import (COMMANDS, DELIVERY, accepts_text, command_text, draft_for, handle,
                     queue, say, nav)


def process_update(update):
    update_id = update.get('update_id')
    if type(update_id) is not int:
        return
    callback = update.get('callback_query')
    msg = callback.get('message', {}) if callback else update.get('message', {})
    sender = callback.get('from', {}) if callback else msg.get('from', {})
    telegram_id, chat_id = str(sender.get('id', '')), str(msg.get('chat', {}).get('id', ''))
    text = str(msg.get('text', '')).strip()[:2500] if not callback else ''
    with db() as conn:
        if conn.execute('SELECT 1 FROM tg_updates WHERE update_id=?', (update_id,)).fetchone():
            return
        conf = settings(conn)
        command, argument = command_text(text, conf)
        authorized_chat = msg.get('chat', {}).get('type') == 'private' or chat_id in conf['telegram_allowed_chats']
        linked = conn.execute('SELECT u.id FROM tg_links t JOIN users u ON u.id=t.user_id WHERE t.telegram_id=? AND u.active=1', (telegram_id,)).fetchone()
        draft = draft_for(conn, telegram_id, chat_id)
        accepted = accepts_text(conn, msg, telegram_id, chat_id, draft)
        codes = {r['currency'] for r in conn.execute('SELECT DISTINCT currency FROM entries')}
        payment_code = None
        if callback and draft:
            parts = callback.get('data', '').split(':')
            payload = json.loads(draft['payload'])
            if len(parts) == 4 and parts[:3] == ['d', draft['token'], 'paycur'] and payload.get('_action') == 'pay':
                row = conn.execute('SELECT currency FROM entries WHERE id=?', (payload['_entry_id'],)).fetchone()
                payment_code = row['currency'] if row else None
    result, error, rates, report = None, None, None, None
    authorized = bool(linked and authorized_chat and command != 'foreign' and not sender.get('is_bot'))
    wants_ai = not callback and (command == '/ai' and bool(argument) or not command and text and accepted and (not draft or draft['step'] == 'ai_text'))
    # External calls happen before taking the SQLite write lock.
    if authorized and wants_ai:
        try:
            result = ai.parse(argument if command == '/ai' else text, conf)
        except HTTPException as exc:
            error = exc.detail
    if authorized:
        codes.add(conf['default_currency'])
        if draft:
            codes.add(json.loads(draft['payload']).get('currency', conf['default_currency']))
        if result:
            codes.add(result.get('currency', conf['default_currency']))
        if payment_code:
            # A payment quote only needs this entry's pair. Other ledger/default
            # currencies must not block a supported settlement-currency payment.
            codes = {payment_code}
        callback_data = callback.get('data', '') if callback else ''
        wants_rates = callback and (callback_data.startswith(('list:', 'e:', 'd:', 'edit:')) or callback_data in ('summary', 'funds'))
        if wants_rates or command in ('/summary', '/team', '/recent', '/funds') or result or (draft and draft['step'].startswith(('edit_', 'description'))):
            try:
                rates = fx.rates_for(conf['reference_currency'], codes)
                if command == '/funds' or (callback and callback.get('data') == 'funds'):
                    report = fx.report()
            except Exception:
                rates = None
    context = DELIVERY.set({'callback': bool(callback), 'message_id': msg.get('message_id'),
                            'user_message_id': msg.get('message_id') if not callback else None,
                            'thread_id': msg.get('message_thread_id')})
    try:
        with db(write=True) as conn:
            if conn.execute('SELECT 1 FROM tg_updates WHERE update_id=?', (update_id,)).fetchone():
                return
            conn.execute('SAVEPOINT handling')
            try:
                handle(conn, update, result, error, rates, report)
            except HTTPException as exc:
                conn.execute('ROLLBACK TO handling')
                if callback:
                    queue(conn, 'answerCallbackQuery', {'callback_query_id': callback['id'], 'text': str(exc.detail)[:150], 'show_alert': True})
                elif chat_id:
                    say(conn, chat_id, str(exc.detail), [nav()])
            conn.execute('RELEASE handling')
            conn.execute('INSERT INTO tg_updates VALUES (?,?)', (update_id, config.now()))
            set_setting(conn, 'bot_offset', update_id + 1)
            set_setting(conn, 'bot_heartbeat', int(time.time()))
    finally:
        DELIVERY.reset(context)


class TelegramError(RuntimeError):
    def __init__(self, code=0, description=''):
        super().__init__(f'Telegram operation failed ({code})')
        self.code, self.description = code, description


def api(method, payload):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    proxy = os.getenv('TELEGRAM_PROXY_URL')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy}) if proxy else urllib.request.ProxyHandler())
    request = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=40) as response:
            body = json.loads(response.read(4_000_000))
        if not body.get('ok'):
            raise TelegramError(body.get('error_code', 0), body.get('description', ''))
        return body.get('result')
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(20000))
        except Exception:
            body = {}
        raise TelegramError(exc.code, body.get('description', '')) from None
    except TelegramError:
        raise
    except Exception:
        # Request URLs contain the token. Never log the original urllib exception.
        raise TelegramError() from None


def drain_outbox():
    with db() as conn:
        pending = [dict(r) for r in conn.execute('SELECT * FROM tg_outbox WHERE sent=0 AND attempts<8 ORDER BY id LIMIT 30')]
    for row in pending:
        payload = json.loads(row['payload'])
        prompt, thread_id = payload.pop('_prompt', None), payload.pop('_thread_id', None)
        method = row['method']
        try:
            try:
                result = api(method, payload)
            except TelegramError as exc:
                if method == 'editMessageText' and exc.code == 400 and 'message is not modified' in exc.description.lower():
                    result = None
                elif method == 'editMessageText' and exc.code == 400:
                    payload.pop('message_id', None)
                    if thread_id:
                        payload['message_thread_id'] = thread_id
                    result = api('sendMessage', payload)
                elif method == 'answerCallbackQuery' and exc.code == 400:
                    result = None
                elif exc.code in (400, 403):
                    with db(write=True) as conn:
                        conn.execute('UPDATE tg_outbox SET attempts=8 WHERE id=?', (row['id'],))
                    continue
                else:
                    raise
            with db(write=True) as conn:
                conn.execute('UPDATE tg_outbox SET sent=1 WHERE id=?', (row['id'],))
                if prompt and result and result.get('message_id'):
                    conn.execute('INSERT OR REPLACE INTO tg_prompts VALUES (?,?,?,?,?,?)',
                                 (str(payload['chat_id']), result['message_id'], prompt['telegram_id'], prompt['token'], prompt['step'], int(time.time()) + 3600))
        except RuntimeError:
            with db(write=True) as conn:
                conn.execute('UPDATE tg_outbox SET attempts=attempts+1 WHERE id=?', (row['id'],))
            break


def configure_commands():
    api('setMyCommands', {'commands': [{'command': c, 'description': d} for c, d in COMMANDS]})
    api('setChatMenuButton', {'menu_button': {'type': 'commands'}})


def main():
    init_db()
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        print('TELEGRAM_BOT_TOKEN 未配置。请填写 .env 后重启。')
        return
    try:
        me = api('getMe', {})
        webhook = api('getWebhookInfo', {})
        if webhook.get('url'):
            print('已配置 Webhook，请先在原服务停用再启动长轮询。')
            return
        with db(write=True) as conn:
            set_setting(conn, 'bot_username', me.get('username'))
            set_setting(conn, 'bot_id', me['id'])
        configure_commands()
        print('Telegram 团队账本已启动。')
    except RuntimeError:
        print('机器人初始化失败，请检查网络与配置。')
        return
    while True:
        try:
            drain_outbox()
            with db() as conn:
                offset = settings(conn).get('bot_offset', 0)
            updates = api('getUpdates', {'offset': offset, 'timeout': 20, 'allowed_updates': ['message', 'callback_query']})
            for update in updates:
                callback = update.get('callback_query')
                if callback:
                    try:
                        api('answerCallbackQuery', {'callback_query_id': callback['id']})
                    except RuntimeError:
                        pass
                process_update(update)
                drain_outbox()
            with db(write=True) as conn:
                set_setting(conn, 'bot_heartbeat', int(time.time()))
        except KeyboardInterrupt:
            break
        except Exception:
            print('机器人连接或处理失败，5 秒后重试。')
            time.sleep(5)


if __name__ == '__main__':
    main()
