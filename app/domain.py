import base64
import hashlib
import hmac
import re
import secrets
from datetime import date
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException
from .config import now
from .db import settings, set_setting


def fail(message, status=400):
    raise HTTPException(status, message)


def text_value(value, label, limit=200, required=True):
    if not isinstance(value, str):
        fail(f'{label}格式不正确')
    value = value.strip()
    if (required and not value) or len(value) > limit:
        fail(f'{label}不能为空且最多 {limit} 个字符' if required else f'{label}最多 {limit} 个字符')
    return value


def hash_password(password):
    if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
        fail('密码长度需为 10–128 个字符')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
    return f'{salt}${digest}'


def verify_password(password, encoded):
    if not isinstance(password, str) or len(password) > 128:
        return False
    salt, expected = encoded.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
    return hmac.compare_digest(actual, expected)


def admin(user):
    if not user['active']:
        fail('账号已停用', 403)


def owner(user):
    admin(user)


def public_user(user):
    return {k: user[k] for k in ('id', 'username', 'name', 'role', 'active')}


def currency(conf, code):
    match = next((c for c in conf['currencies'] if c['code'] == code), None)
    if not match:
        fail('请选择团队已启用的币种')
    return match


def minor_amount(value, decimals):
    value = str(value).strip()
    if not re.fullmatch(r'\d{1,12}(?:\.\d{1,4})?', value):
        fail('金额必须为正数，不支持千分位或科学计数法')
    try:
        amount = Decimal(value)
        scaled = amount * (10 ** decimals)
        if amount <= 0 or scaled != scaled.to_integral_value() or scaled > 9_000_000_000_000:
            fail(f'金额超出范围或小数超过 {decimals} 位')
        return int(scaled)
    except InvalidOperation:
        fail('金额格式不正确')


def normalized_entry(conn, user, body):
    conf = settings(conn)
    kind = body.get('kind', 'expense')
    if kind not in ('expense', 'income'):
        fail('账目类型不正确')
    if kind == 'income':
        admin(user)
    cur = currency(conf, body.get('currency', conf['default_currency']))
    amount = minor_amount(body.get('amount', ''), cur['decimals'])
    description = text_value(body.get('description', ''), '用途 / 收入说明', 120)
    try:
        entry_date = date.fromisoformat(body.get('date', ''))
        if entry_date.year < 2000 or entry_date.year > 2100:
            raise ValueError()
    except (ValueError, TypeError):
        fail('日期格式不正确')
    payer_id = body.get('payer_id', user['id'])
    if not isinstance(payer_id, int):
        fail('成员格式不正确')
    if payer_id != user['id']:
        admin(user)
    if not conn.execute('SELECT id FROM users WHERE id=? AND active=1', (payer_id,)).fetchone():
        fail('成员不存在或已停用')
    status = body.get('status', 'submitted') if kind == 'expense' else 'posted'
    if status not in (('draft', 'submitted') if kind == 'expense' else ('posted',)):
        fail('新支出只能保存为草稿或提交报销')
    return {'kind': kind, 'amount_minor': amount, 'currency': cur['code'], 'decimals': cur['decimals'],
            'description': description, 'category': '', 'date': entry_date.isoformat(),
            'payer_id': payer_id, 'status': status, 'note': text_value(body.get('note', ''), '备注', 1000, False)}


def entry_view(conn, entry_id):
    row = conn.execute('SELECT e.*,u.name payer_name,c.name creator_name FROM entries e JOIN users u ON u.id=e.payer_id JOIN users c ON c.id=e.created_by WHERE e.id=?', (entry_id,)).fetchone()
    if not row:
        fail('账目不存在', 404)
    result = dict(row)
    result['amount'] = format(Decimal(row['amount_minor']) / (10 ** row['decimals']), f'.{row["decimals"]}f')
    result['receipt'] = bool(result['receipt'])
    result.pop('category', None)  # Retained database column only for compatibility with earlier local builds.
    return result


def audit(conn, entry_id, user_id, action, detail=''):
    conn.execute('INSERT INTO events(entry_id,user_id,action,detail,created_at) VALUES (?,?,?,?,?)',
                 (entry_id, user_id, action, detail, now()))


def add_entry(conn, user, body, source='web', source_key=None):
    data = normalized_entry(conn, user, body)
    if not source_key or len(source_key) > 200:
        fail('缺少有效的防重复提交标识')
    existing = conn.execute('SELECT id,created_by FROM entries WHERE source_key=?', (source_key,)).fetchone()
    if existing:
        if existing['created_by'] != user['id']:
            fail('重复提交标识冲突', 409)
        previous = entry_view(conn, existing['id'])
        if any(previous[k] != v for k, v in data.items() if k not in ('status', 'category')):
            fail('同一提交标识不能用于不同账目', 409)
        return previous
    timestamp = now()
    cols = list(data) + ['created_by', 'source', 'source_key', 'created_at', 'updated_at']
    values = list(data.values()) + [user['id'], source, source_key, timestamp, timestamp]
    cursor = conn.execute(f'INSERT INTO entries({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})', values)
    audit(conn, cursor.lastrowid, user['id'], 'created', f'通过 {source} 创建')
    return entry_view(conn, cursor.lastrowid)


def transition(conn, user, entry_id, action, note=''):
    entry = entry_view(conn, entry_id)
    if action == 'void':
        if entry['kind'] == 'income' or entry['payer_id'] != user['id'] or entry['status'] == 'approved':
            admin(user)
        if entry['status'] in ('reimbursed', 'voided'):
            fail('已报销或已作废账目不能再次作废', 409)
        note = text_value(note, '作废原因', 500)
        conn.execute('UPDATE entries SET status=?,review_note=?,updated_at=? WHERE id=?', ('voided', note, now(), entry_id))
        audit(conn, entry_id, user['id'], 'void', note)
        return entry_view(conn, entry_id)
    if entry['kind'] != 'expense':
        fail('收入无需报销')
    note = text_value(note, '审核备注', 500, False)
    transitions = {'submit': (('draft', 'rejected'), 'submitted'),
                   'approve': (('submitted',), 'approved'),
                   'reject': (('submitted',), 'rejected'),
                   'pay': (('approved',), 'reimbursed')}
    if action not in transitions:
        fail('操作不正确')
    if action == 'submit':
        if entry['payer_id'] != user['id']:
            admin(user)
    else:
        admin(user)
    allowed, target = transitions[action]
    if entry['status'] not in allowed:
        fail('账目状态已变化，请刷新后重试', 409)
    if action == 'reject' and not note:
        fail('请填写驳回原因')
    if action == 'pay' and not note:
        fail('请填写付款方式或付款参考号')
    paid_at = now() if action == 'pay' else None
    conn.execute('UPDATE entries SET status=?,review_note=?,paid_at=?,updated_at=? WHERE id=?',
                 (target, note, paid_at, now(), entry_id))
    audit(conn, entry_id, user['id'], action, note)
    return entry_view(conn, entry_id)


def edit_entry(conn, user, entry_id, body):
    old = entry_view(conn, entry_id)
    if old['kind'] != 'expense' or old['status'] not in ('draft', 'rejected'):
        fail('仅草稿或已驳回的支出可以修改', 409)
    if old['payer_id'] != user['id']:
        admin(user)
    data = normalized_entry(conn, user, body)
    if data['kind'] != old['kind']:
        fail('不支持更改账目类型')
    conn.execute('UPDATE entries SET ' + ','.join(f'{k}=?' for k in data) + ',review_note=?,updated_at=? WHERE id=?',
                 list(data.values()) + ['', now(), entry_id])
    audit(conn, entry_id, user['id'], 'edited')
    return entry_view(conn, entry_id)


def save_settings(conn, user, body):
    owner(user)
    conf = settings(conn)
    name = text_value(body.get('team_name', conf['team_name']), '团队名称', 40)
    currencies = body.get('currencies', conf['currencies'])
    if not isinstance(currencies, list) or not 1 <= len(currencies) <= 30:
        fail('请配置 1–30 个币种')
    codes = set()
    clean = []
    for cur in currencies:
        if not isinstance(cur, dict) or not re.fullmatch('[A-Z]{3}', str(cur.get('code', ''))):
            fail('币种代码必须为 3 位大写英文字母')
        code = cur['code']
        decimals = cur.get('decimals', 2)
        if type(decimals) is not int or decimals not in (0, 2, 3, 4) or code in codes:
            fail('币种不能重复，小数位支持 0、2、3、4')
        used = conn.execute('SELECT decimals FROM entries WHERE currency=? LIMIT 1', (code,)).fetchone()
        if used and used['decimals'] != decimals:
            fail(f'{code} 已有账目，不能修改其小数位')
        codes.add(code)
        clean.append({'code': code, 'decimals': decimals, 'name': text_value(cur.get('name', code), '币种名称', 20),
                      'symbol': text_value(cur.get('symbol', code), '货币符号', 8)})
    for row in conn.execute('SELECT DISTINCT currency FROM entries'):
        if row['currency'] not in codes:
            fail(f'{row["currency"]} 已有账目，不能移除')
    default = body.get('default_currency', conf['default_currency'])
    reference = body.get('reference_currency', conf['reference_currency'])
    if default not in codes or reference not in codes:
        fail('默认币种和汇总币种必须在启用币种中')
    chats = body.get('telegram_allowed_chats', conf['telegram_allowed_chats'])
    if not isinstance(chats, list) or len(chats) > 30 or any(not re.fullmatch(r'-?\d{1,20}', str(c)) for c in chats):
        fail('Telegram 群组 ID 格式不正确')
    values = dict(team_name=name, currencies=clean, default_currency=default, reference_currency=reference,
                  telegram_allowed_chats=list(dict.fromkeys(str(c) for c in chats)))
    for key, value in values.items():
        set_setting(conn, key, value)
    return settings(conn)
