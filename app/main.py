import base64
import csv
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from . import config, ai, fx
from .db import db, init_db, settings
from .domain import (fail, admin, owner, public_user, hash_password, verify_password,
                     text_value, add_entry, edit_entry, transition, entry_view, audit, save_settings, make_payment_quote)

PUBLIC = config.ROOT / 'public'
DUMMY_HASH = hash_password('unused-dummy-password')
attempts = defaultdict(deque)
attempt_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title='一起记 · 团队账本', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse({'error': exc.detail}, status_code=exc.status_code)


@app.middleware('http')
async def security(request, call_next):
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('origin')
        accepted = {str(request.base_url).rstrip('/')}
        if os.getenv('PUBLIC_URL'):
            accepted.add(os.environ['PUBLIC_URL'].rstrip('/'))
        if origin and origin.rstrip('/') not in accepted:
            return JSONResponse({'error': '请求来源不正确'}, status_code=403)
        if int(request.headers.get('content-length', '0') or 0) > 9_000_000:
            return JSONResponse({'error': '上传内容过大'}, status_code=413)
        total = 0
        chunks = []
        async for chunk in request.stream():
            total += len(chunk)
            if total > 9_000_000:
                return JSONResponse({'error': '上传内容过大'}, status_code=413)
            chunks.append(chunk)
        request._body = b''.join(chunks)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def limited(request, scope, maximum=12):
    key = (scope, request.client.host if request.client else 'local')
    with attempt_lock:
        cutoff = time.time() - 300
        # Evict inactive clients so the limiter cannot grow indefinitely.
        if len(attempts) > 5000:
            for old in list(attempts):
                if not attempts[old] or attempts[old][-1] < cutoff:
                    del attempts[old]
        queue = attempts[key]
        while queue and queue[0] < cutoff:
            queue.popleft()
        if len(queue) >= maximum:
            fail('操作过于频繁，请稍后再试', 429)
        queue.append(time.time())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def current(request):
    token = request.cookies.get('ledger_session', '')
    if not token:
        fail('请先登录', 401)
    with db() as conn:
        row = conn.execute('SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1',
                           (digest(token), int(time.time()))).fetchone()
    if not row:
        fail('登录已失效，请重新登录', 401)
    if request.method not in ('GET', 'HEAD') and not secrets.compare_digest(request.headers.get('x-csrf-token', ''), row['csrf']):
        fail('安全校验已失效，请刷新页面', 403)
    return dict(row)


def new_session(conn, user_id, response):
    token = secrets.token_urlsafe(36)
    csrf = secrets.token_urlsafe(32)
    conn.execute('DELETE FROM sessions WHERE expires<?', (int(time.time()),))
    conn.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(token), user_id, csrf, int(time.time()) + 7 * 86400))
    response.set_cookie('ledger_session', token, httponly=True, samesite='lax', max_age=7 * 86400,
                        secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true', path='/')
    return csrf


def make_user(conn, body, role=None):
    username = text_value(body.get('username', ''), '用户名', 32)
    if not re.fullmatch('[a-zA-Z0-9_.-]{3,32}', username):
        fail('用户名需为 3–32 位英文字母、数字、点、横线或下划线')
    name = text_value(body.get('name', ''), '成员姓名', 30)
    password = hash_password(body.get('password', ''))
    # Flat team: every enabled account has the same management permissions.
    role = 'owner'
    if conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
        fail('用户名已被使用', 409)
    cursor = conn.execute('INSERT INTO users(username,name,password_hash,role,created_at) VALUES (?,?,?,?,?)',
                          (username, name, password, role, config.now()))
    return conn.execute('SELECT * FROM users WHERE id=?', (cursor.lastrowid,)).fetchone()


@app.get('/api/health')
def health():
    return {'ok': True}


@app.get('/api/bootstrap')
def bootstrap_status():
    with db() as conn:
        return {'needs_setup': not bool(conn.execute('SELECT id FROM users LIMIT 1').fetchone())}


@app.post('/api/bootstrap')
def bootstrap(body: dict, request: Request, response: Response):
    limited(request, 'bootstrap', 8)
    with db(write=True) as conn:
        if conn.execute('SELECT id FROM users LIMIT 1').fetchone():
            fail('团队已初始化，请登录', 409)
        user = make_user(conn, body, 'owner')
        conf_body = {'team_name': body.get('team_name', '一起记')}
        save_settings(conn, user, conf_body)
        csrf = new_session(conn, user['id'], response)
        return {'user': public_user(user), 'csrf': csrf}


@app.post('/api/login')
def login(body: dict, request: Request, response: Response):
    limited(request, 'login')
    username = str(body.get('username', ''))[:32]
    with db(write=True) as conn:
        user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        valid = verify_password(body.get('password', ''), user['password_hash'] if user else DUMMY_HASH)
        if not user or not valid or not user['active']:
            fail('用户名或密码不正确', 401)
        csrf = new_session(conn, user['id'], response)
        return {'user': public_user(user), 'csrf': csrf}


@app.get('/api/session')
def session(request: Request):
    user = current(request)
    return {'user': public_user(user), 'csrf': user['csrf'], 'today': config.today()}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    current(request)
    with db(write=True) as conn:
        conn.execute('DELETE FROM sessions WHERE token_hash=?', (digest(request.cookies.get('ledger_session', '')),))
    response.delete_cookie('ledger_session', path='/')
    return {'ok': True}


@app.post('/api/password')
def password(body: dict, request: Request, response: Response):
    user = current(request)
    limited(request, 'password')
    if not verify_password(body.get('old_password', ''), user['password_hash']):
        fail('原密码不正确')
    encoded = hash_password(body.get('password', ''))
    with db(write=True) as conn:
        conn.execute('UPDATE users SET password_hash=? WHERE id=?', (encoded, user['id']))
        conn.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
        csrf = new_session(conn, user['id'], response)
    return {'ok': True, 'csrf': csrf}


@app.get('/api/settings')
def get_settings(request: Request):
    current(request)
    with db() as conn:
        conf = settings(conn)
    return {**conf, 'today': config.today(), 'timezone': config.TIMEZONE}


@app.put('/api/settings')
def put_settings(body: dict, request: Request):
    user = current(request)
    with db(write=True) as conn:
        return save_settings(conn, user, body)


@app.get('/api/members')
def members(request: Request):
    current(request)
    with db() as conn:
        return [dict(r) for r in conn.execute('SELECT u.id,u.username,u.name,u.role,u.active,CASE WHEN t.telegram_id IS NULL THEN 0 ELSE 1 END telegram_linked FROM users u LEFT JOIN tg_links t ON t.user_id=u.id ORDER BY u.id')]


@app.post('/api/members')
def add_member(body: dict, request: Request):
    user = current(request)
    owner(user)
    with db(write=True) as conn:
        return public_user(make_user(conn, body))


@app.patch('/api/members/{member_id}')
def update_member(member_id: int, body: dict, request: Request):
    user = current(request)
    owner(user)
    with db(write=True) as conn:
        target = conn.execute('SELECT * FROM users WHERE id=?', (member_id,)).fetchone()
        if not target:
            fail('成员不存在', 404)
        active = body.get('active', target['active'])
        if type(active) not in (int, bool) or active not in (0, 1):
            fail('成员状态不正确')
        role = 'owner'
        if target['role'] == 'owner' and target['active'] and (role != 'owner' or not active):
            others = conn.execute("SELECT COUNT(*) FROM users WHERE role='owner' AND active=1 AND id<>?", (member_id,)).fetchone()[0]
            if not others:
                fail('必须保留至少一位启用的团队成员')
        conn.execute('UPDATE users SET active=?,role=? WHERE id=?', (int(active), role, member_id))
        if 'password' in body:
            conn.execute('UPDATE users SET password_hash=? WHERE id=?', (hash_password(body['password']), member_id))
        if not active or 'password' in body:
            conn.execute('DELETE FROM sessions WHERE user_id=?', (member_id,))
        return {'ok': True}


@app.get('/api/entries')
def entries(request: Request):
    current(request)
    with db() as conn:
        ids = conn.execute('SELECT id FROM entries ORDER BY date DESC,id DESC').fetchall()
        return [entry_view(conn, r['id']) for r in ids]


@app.post('/api/entries')
def create_entry(body: dict, request: Request):
    user = current(request)
    key = request.headers.get('idempotency-key', '')
    if not re.fullmatch('[A-Za-z0-9_-]{16,100}', key):
        fail('缺少有效的防重复提交标识')
    with db(write=True) as conn:
        return add_entry(conn, user, body, source_key=f'web:{user["id"]}:{key}')


@app.put('/api/entries/{entry_id}')
def update_entry(entry_id: int, body: dict, request: Request):
    user = current(request)
    with db(write=True) as conn:
        return edit_entry(conn, user, entry_id, body)


@app.post('/api/entries/action')
def entry_action(body: dict, request: Request):
    user = current(request)
    ids = body.get('ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
        fail('请选择 1–100 笔不同账目')
    payment_data, prefix = {}, ''
    if body.get('action') == 'pay':
        key = request.headers.get('idempotency-key', '')
        if not re.fullmatch('[A-Za-z0-9_-]{16,100}', key):
            fail('缺少付款防重复标识，请刷新页面后重试')
        payments = body.get('payments')
        if not isinstance(payments, list) or len(payments) != len(ids):
            fail('请逐笔填写本次报销金额，旧页面请刷新后重试')
        for payment in payments:
            if not isinstance(payment, dict) or type(payment.get('id')) is not int or not isinstance(payment.get('quote_id'), str):
                fail('付款项目格式不正确，请刷新后重试')
            if payment['id'] in payment_data:
                fail('付款项目不能重复')
            payment_data[payment['id']] = payment
        if set(payment_data) != set(ids):
            fail('付款项目与所选账目不一致')
        prefix = f'pay:web:{user["id"]}:{key}:'
    with db(write=True) as conn:
        # Entire batch is one transaction: a stale or unauthorized row rolls back all rows.
        if prefix:
            previous = conn.execute('SELECT entry_id FROM payments WHERE substr(source_key,1,?)=?', (len(prefix), prefix)).fetchall()
            if previous and {r['entry_id'] for r in previous} != set(ids):
                fail('同一付款标识不能用于不同的付款项目', 409)
        return [transition(conn, user, entry_id, body.get('action'), body.get('note', ''),
                           **({'amount': payment_data[entry_id].get('amount'), 'payment_key': prefix + str(entry_id),
                               'quote_id': payment_data[entry_id]['quote_id']} if prefix else {})) for entry_id in ids]


@app.post('/api/entries/payment-quotes')
def payment_quotes(body: dict, request: Request):
    user = current(request)
    ids = body.get('ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
        fail('请选择 1–100 笔不同账目')
    with db() as conn:
        conf = settings(conn)
        codes = {entry_view(conn, entry_id)['currency'] for entry_id in ids}
    code = body.get('currency', 'original')
    rates = fx.rates_for(conf['reference_currency'], codes) if code == conf['reference_currency'] else None
    with db(write=True) as conn:
        return [make_payment_quote(conn, user, entry_id, code, rates) for entry_id in ids]


@app.get('/api/entries/{entry_id}/events')
def events(entry_id: int, request: Request):
    current(request)
    with db() as conn:
        entry_view(conn, entry_id)
        return [dict(r) for r in conn.execute('SELECT e.*,u.name FROM events e JOIN users u ON u.id=e.user_id WHERE entry_id=? ORDER BY e.id', (entry_id,))]


@app.post('/api/entries/{entry_id}/receipt')
def upload_receipt(entry_id: int, body: dict, request: Request):
    user = current(request)
    try:
        data = base64.b64decode(body.get('data', ''), validate=True)
    except Exception:
        fail('凭证文件格式不正确')
    if not data or len(data) > 6 * 1024 * 1024:
        fail('凭证大小需小于 6 MB')
    suffix = '.png' if data.startswith(b'\x89PNG\r\n\x1a\n') else '.jpg' if data.startswith(b'\xff\xd8\xff') else '.pdf' if data.startswith(b'%PDF-') else None
    if not suffix:
        fail('仅支持 PNG、JPEG 和 PDF 凭证')
    name = text_value(body.get('name', '凭证' + suffix), '文件名', 150)
    with db(write=True) as conn:
        entry = conn.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()
        if not entry:
            fail('账目不存在', 404)
        if entry['payer_id'] != user['id']:
            admin(user)
        if entry['status'] not in ('draft', 'pending', 'posted', 'distributed'):
            fail('已报销或已作废的账目不能替换凭证', 409)
        folder = config.DATA / 'receipts'
        folder.mkdir(exist_ok=True)
        filename = secrets.token_hex(24) + suffix
        (folder / filename).write_bytes(data)
        try:
            conn.execute('UPDATE entries SET receipt=?,receipt_name=?,updated_at=? WHERE id=?', (filename, name, config.now(), entry_id))
            audit(conn, entry_id, user['id'], 'receipt', '上传凭证')
        except Exception:
            (folder / filename).unlink(missing_ok=True)
            raise
        old = entry['receipt']
    if old:
        (config.DATA / 'receipts' / old).unlink(missing_ok=True)
    return {'ok': True}


@app.get('/api/entries/{entry_id}/receipt')
def get_receipt(entry_id: int, request: Request):
    current(request)
    with db() as conn:
        entry = conn.execute('SELECT receipt,receipt_name FROM entries WHERE id=?', (entry_id,)).fetchone()
    if not entry or not entry['receipt']:
        fail('未上传凭证', 404)
    path = config.DATA / 'receipts' / entry['receipt']
    if not path.is_file():
        fail('凭证文件不存在，请检查备份', 404)
    return FileResponse(path, filename=entry['receipt_name'])


@app.get('/api/report')
def get_report(request: Request, base: str | None = None):
    current(request)
    return fx.report(base)


@app.post('/api/calculate')
def calculate(body: dict, request: Request):
    current(request)
    return fx.report(body.get('base'), {'amount': body.get('amount', ''), 'currency': body.get('currency')})


@app.post('/api/fx/refresh')
def refresh_fx(request: Request):
    current(request)
    limited(request, 'fx', 5)
    return fx.report(force=True)


@app.post('/api/ai/parse')
def ai_parse(body: dict, request: Request):
    user = current(request)
    limited(request, 'ai', 20)
    with db() as conn:
        conf = settings(conn)
    result = ai.parse(body.get('text'), conf)
    from .domain import normalized_entry
    with db() as conn:
        normalized_entry(conn, user, result)
    return result


@app.get('/api/integrations')
def integrations(request: Request):
    user = current(request)
    with db() as conn:
        conf = settings(conn)
        linked = conn.execute('SELECT telegram_id FROM tg_links WHERE user_id=?', (user['id'],)).fetchone()
    heartbeat = conf.get('bot_heartbeat', 0)
    return {'ai_configured': ai.configured(), 'bot_configured': bool(os.getenv('TELEGRAM_BOT_TOKEN')),
            'bot_online': time.time() - heartbeat < 90, 'bot_username': conf.get('bot_username'),
            'telegram_linked': bool(linked)}


@app.post('/api/telegram/link-code')
def link_code(request: Request):
    user = current(request)
    limited(request, 'link', 10)
    code = secrets.token_urlsafe(12)
    with db(write=True) as conn:
        conn.execute('DELETE FROM link_codes WHERE user_id=? OR expires<?', (user['id'], int(time.time())))
        conn.execute('INSERT INTO link_codes VALUES (?,?,?)', (digest(code), user['id'], int(time.time()) + 600))
    return {'code': code, 'expires_in': 600}


@app.post('/api/telegram/unlink')
def unlink(request: Request):
    user = current(request)
    with db(write=True) as conn:
        conn.execute('DELETE FROM tg_links WHERE user_id=?', (user['id'],))
        conn.execute('DELETE FROM link_codes WHERE user_id=?', (user['id'],))
        conn.execute('DELETE FROM tg_drafts WHERE user_id=?', (user['id'],))
    return {'ok': True}


@app.get('/api/export')
def export(request: Request):
    current(request)
    with db() as conn:
        rows = [entry_view(conn, r['id']) for r in conn.execute('SELECT id FROM entries ORDER BY date,id').fetchall()]
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['编号', '类型', '日期', '用途', '成员', '金额（原币种）', '币种', '状态', '来源', '备注', '处理/付款备注', '最近付款时间', '累计已报销（原币种）', '剩余待报销（原币种）', '逐次付款明细'])
    from decimal import Decimal
    def safe(value):
        value = str(value or '')
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value
    for row in rows:
        amount = format(Decimal(row['amount_minor']) / (10 ** row['decimals']), f'.{row["decimals"]}f')
        paid = format(Decimal(row['reimbursed_minor']) / (10 ** row['decimals']), f'.{row["decimals"]}f')
        due = format(Decimal(row['amount_minor'] - row['reimbursed_minor']) / (10 ** row['decimals']), f'.{row["decimals"]}f') if row['kind'] == 'expense' and row['status'] == 'pending' else ''
        writer.writerow([row['id'], row['kind'], row['date'], safe(row['description']), safe(row['payer_name']),
                         amount, row['currency'], '部分报销' if row['status'] == 'pending' and row['reimbursed_minor'] else row['status'], row['source'], safe(row['note']), safe(row['review_note']), row['paid_at'], paid, due,
                         safe(json.dumps(row['payments'], ensure_ascii=False)) if row['payments'] else ''])
    return StreamingResponse(iter([output.getvalue()]), media_type='text/csv; charset=utf-8',
                             headers={'Content-Disposition': 'attachment; filename="team-ledger.csv"'})


@app.get('/')
def index():
    html = (PUBLIC / 'index.html').read_text(encoding='utf-8')
    for asset in ('app.js', 'style.css'):
        version = hashlib.sha256((PUBLIC / asset).read_bytes()).hexdigest()[:12]
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={version}"')
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


app.mount('/static', StaticFiles(directory=PUBLIC), name='static')
