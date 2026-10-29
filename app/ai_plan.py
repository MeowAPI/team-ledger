"""Validation, dry-run previews and atomic execution for AI bookkeeping plans."""
import json
import re
import secrets
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException
from . import config, dividends as dividend_service, fx
from .db import settings
from .domain import (add_entry, currency, delete_entry, edit_entry, entry_view, fail, make_payment_quote,
                     minor_amount, normalized_entry, payment_preview, text_value, transition)
from .funds_plan import build_plan as build_funds_plan, snapshot as funds_snapshot

PLAN_TTL = 1800
MAX_RESOLVED = 50


class PlanQuestion(Exception):
    """The action needs one more detail from the user; do not create a card."""


def _card(kind, label):
    return {'index': None, 'type': kind, 'writable': True, 'selected': True,
            'label': label, 'summary': '', 'resolved': {}, 'errors': [], 'warnings': []}


def _error(card, exc):
    card['errors'].append(str(exc.detail) if isinstance(exc, HTTPException) else str(exc))


def _text(value, limit, default=''):
    try:
        return text_value(value if value is not None else default, '内容', limit, False)
    except HTTPException as exc:
        raise PlanQuestion(exc.detail)


def _date_value(value, default=None):
    """Accept ISO plus common Chinese/relative forms; return ISO or default."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    text = str(value).strip()
    today = date.fromisoformat(config.today())
    words = {'今天': 0, '今日': 0, '昨天': -1, '昨日': -1, '前天': -2, '明天': 1, '明日': 1, '后天': 2}
    if text in words:
        return (today + timedelta(days=words[text])).isoformat()
    weekdays = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}
    match = re.fullmatch(r'(上|本|这)(?:周|星期)([一二三四五六日天])', text)
    if match:
        monday = today - timedelta(days=today.weekday())
        target = monday + timedelta(days=weekdays[match.group(2)])
        if match.group(1) == '上':
            target -= timedelta(days=7)
        return target.isoformat()
    match = re.fullmatch(r'(?:周|星期)([一二三四五六日天])', text)
    if match:
        monday = today - timedelta(days=today.weekday())
        return (monday + timedelta(days=weekdays[match.group(1)])).isoformat()
    text = text.replace('年', '-').replace('月', '-').replace('日', '').replace('号', '')
    text = text.replace('/', '-').replace('.', '-').strip('-')
    match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return default
    match = re.fullmatch(r'(\d{1,2})-(\d{1,2})', text)
    if match:
        try:
            candidate = date(today.year, int(match.group(1)), int(match.group(2)))
            if candidate > today + timedelta(days=31):
                candidate = date(today.year - 1, candidate.month, candidate.day)
            return candidate.isoformat()
        except ValueError:
            return default
    return default


def _strict_date(value, label):
    if value in (None, ''):
        return None
    parsed = _date_value(value)
    if not parsed:
        raise PlanQuestion(f'{label}格式不正确，请使用 YYYY-MM-DD。')
    return parsed


def _amount_value(value):
    """Return a plain decimal string, 'FULL' for full-amount requests, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = format(Decimal(str(value)), 'f')
    else:
        text = str(value).strip()
        text = text.translate(str.maketrans('０１２３４５６７８９．，', '0123456789.,'))
        lowered = text.lower()
        if any(word in lowered for word in ('full', 'all remaining', '全部', '全额', '结清', '剩余全部')):
            return 'FULL'
        unit = Decimal(10000) if '万' in text else Decimal(1000) if ('千' in text or re.search(r'\d\s*[kK]\b', text)) else Decimal(1)
        text = text.replace(',', '')
        match = re.search(r'-?\d+(?:\.\d+)?', text)
        if not match:
            return None
        try:
            number = Decimal(match.group(0)) * unit
        except InvalidOperation:
            return None
        text = format(number, 'f')
    return text or None


def _money(amount, code, conf):
    cur = next((c for c in conf['currencies'] if c['code'] == code), None)
    if not cur:
        return f'{amount} {code}'
    try:
        value = Decimal(str(amount)).quantize(Decimal(1).scaleb(-cur['decimals']))
    except (InvalidOperation, ValueError):
        return f'{amount} {code}'
    return f'{cur["symbol"]}{value:,.{cur["decimals"]}f} {code}'


def _entry_desc(entry):
    return entry.get('description') or '未填写说明'


def _members(conn):
    return [dict(r) for r in conn.execute('SELECT id,name,username FROM users WHERE active=1 ORDER BY id')]


def _member(conn, value, user):
    if value in (None, '', '我', '本人', '自己'):
        return user['id'], user['name']
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        row = conn.execute('SELECT id,name FROM users WHERE id=? AND active=1', (int(value),)).fetchone()
        if row:
            return row['id'], row['name']
        raise PlanQuestion(f'没有找到成员 #{value}，请确认。')
    text = str(value).strip().lstrip('@')
    rows = _members(conn)
    exact = [r for r in rows if text in (r['name'], r['username'])]
    if len(exact) == 1:
        return exact[0]['id'], exact[0]['name']
    lowered = text.casefold()
    exact = [r for r in rows if lowered in (r['name'].casefold(), r['username'].casefold())]
    if len(exact) == 1:
        return exact[0]['id'], exact[0]['name']
    partial = [r for r in rows if lowered in r['name'].casefold() or lowered in r['username'].casefold()]
    if len(partial) == 1:
        return partial[0]['id'], partial[0]['name']
    if len(partial) > 1:
        names = '、'.join(r['name'] for r in partial[:5])
        raise PlanQuestion(f'成员「{text}」可能指 {names}，请写全名。')
    raise PlanQuestion(f'没有找到成员「{text}」，请确认姓名。')


def _target(conn, value, context_ids):
    try:
        entry_id = int(value)
    except (TypeError, ValueError):
        raise PlanQuestion('请指明要处理的账目编号。')
    try:
        entry = entry_view(conn, entry_id)
    except HTTPException:
        raise PlanQuestion(f'没有找到账目 #{entry_id}，请核对编号。')
    warning = None if entry_id in context_ids else f'#{entry_id} 不在最近摘要中，请核对后再确认。'
    return entry, warning


def _ids(action):
    values = action.get('ids')
    if isinstance(values, list):
        result = []
        for value in values:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                continue
        if result:
            return list(dict.fromkeys(result))
    if action.get('id') is not None:
        try:
            return [int(action['id'])]
        except (TypeError, ValueError):
            return []
    return []


def _append(actions, card):
    if len(actions) >= MAX_RESOLVED:
        fail(f'一次最多处理 {MAX_RESOLVED} 项操作，请分成几段描述。')
    actions.append(card)


# --- individual action resolvers -------------------------------------------

def _resolve_create(conn, user, conf, action):
    card = _card('create_entry', '新增账目')
    kind = action.get('kind') if action.get('kind') in ('expense', 'income') else 'expense'
    if action.get('kind') and action.get('kind') not in ('expense', 'income'):
        card['errors'].append('账目类型只能是支出或收入')
    code = action.get('currency') or conf['default_currency']
    try:
        cur = currency(conf, code)
        code = cur['code']
    except HTTPException as exc:
        _error(card, exc)
        cur = currency(conf, conf['default_currency'])
        code = cur['code']
    amount = _amount_value(action.get('amount'))
    if amount == 'FULL':
        card['errors'].append('新增账目需要明确金额')
        amount = None
    if amount is None:
        card['errors'].append('缺少金额')
    else:
        try:
            minor_amount(amount, cur['decimals'])
        except HTTPException as exc:
            _error(card, exc)
    description = str(action.get('description') or '').strip()
    if len(description) > 120:
        card['errors'].append('用途最多 120 个字符')
        description = description[:120]
    if action.get('date') in (None, ''):
        entry_date = config.today()
    else:
        entry_date = _date_value(action['date'], None)
        if not entry_date:
            card['errors'].append('日期格式不正确，请使用 YYYY-MM-DD。')
            entry_date = config.today()
    payer_id, payer_name = _member(conn, action.get('payer'), user)
    requested_status = action.get('status')
    if kind == 'income':
        status = 'posted'
        if requested_status == 'distributed' or action.get('income_distributed') is True:
            card['warnings'].append('收入统一记为团队在账；分红请单独登记盈余分红。')
    else:
        if requested_status not in (None, '', 'draft', 'pending'):
            card['errors'].append('支出状态只能是草稿或待报销')
        status = 'draft' if requested_status == 'draft' else 'pending'
    try:
        note = _text(action.get('note'), 1000)
    except PlanQuestion as exc:
        card['errors'].append(str(exc))
        note = ''
    body = {'kind': kind, 'amount': amount or '', 'currency': code, 'description': description,
            'date': entry_date, 'payer_id': payer_id, 'status': status, 'note': note}
    if not card['errors']:
        try:
            normalized_entry(conn, user, body)
        except HTTPException as exc:
            _error(card, exc)
    label = '新增收入' if kind == 'income' else ('新增草稿' if status == 'draft' else '新增支出')
    card['label'] = label
    card['resolved'] = {'kind': kind, 'amount': amount or '', 'currency': code, 'description': description,
                        'date': entry_date, 'payer_id': payer_id, 'payer_name': payer_name,
                        'status': status, 'note': note}
    state = '草稿' if status == 'draft' else '团队在账' if kind == 'income' else '待报销'
    card['summary'] = f'{description or "未填写说明"} · {_money(amount or 0, code, conf)} · {entry_date} · {payer_name} · {state}'
    return card


def _resolve_edit(conn, user, conf, action, context_ids):
    card = _card('edit_entry', '修改账目')
    entry, warning = _target(conn, action.get('id'), context_ids)
    if warning:
        card['warnings'].append(warning)
    if entry['kind'] != 'expense' or entry['status'] not in ('draft', 'pending') or entry['reimbursed_minor']:
        card['errors'].append('仅没有报销记录的草稿或待报销支出可以修改')
        card['summary'] = f'#{entry["id"]} {_entry_desc(entry)} · 当前状态不允许修改'
        card['resolved'] = {'id': entry['id'], 'version': entry['version']}
        return card
    if action.get('kind') and action.get('kind') != entry['kind']:
        card['errors'].append('不支持更改账目类型')
    body = {'kind': entry['kind'], 'amount': entry['amount'], 'currency': entry['currency'],
            'description': entry['description'], 'date': entry['date'], 'payer_id': entry['payer_id'],
            'note': entry['note'], 'status': entry['status']}
    if 'amount' in action:
        amount = _amount_value(action.get('amount'))
        if amount == 'FULL' or amount is None:
            card['errors'].append('修改金额需要明确数字')
        else:
            body['amount'] = amount
    if 'currency' in action and action.get('currency'):
        try:
            body['currency'] = currency(conf, action['currency'])['code']
        except HTTPException as exc:
            _error(card, exc)
    if 'description' in action:
        description = str(action.get('description') or '').strip()
        if len(description) > 120:
            card['errors'].append('用途最多 120 个字符')
        else:
            body['description'] = description
    if 'date' in action and action.get('date'):
        body['date'] = _date_value(action['date'], '')
        if not body['date']:
            card['errors'].append('日期格式不正确')
    payer_name = entry['payer_name']
    if 'payer' in action and action.get('payer') is not None:
        payer_id, payer_name = _member(conn, action['payer'], user)
        body['payer_id'] = payer_id
    if 'note' in action:
        body['note'] = str(action.get('note') or '')[:1000]
    if not card['errors']:
        try:
            normalized_entry(conn, user, body)
        except HTTPException as exc:
            _error(card, exc)
    card['resolved'] = {'id': entry['id'], 'version': entry['version'], **body, 'payer_name': payer_name}
    before = f'{_money(entry["amount"], entry["currency"], conf)} · {entry["description"] or "未填写说明"}'
    after = f'{_money(body["amount"], body["currency"], conf)} · {body["description"] or "未填写说明"}'
    card['summary'] = f'#{entry["id"]} 修改：{before} → {after} · {body["date"]}'
    return card


def _resolve_transition(conn, user, conf, action, context_ids):
    kind = action['type']
    labels = {'void_entry': '作废账目', 'submit_entry': '草稿入账', 'distribute_income': '收入分红'}
    card = _card(kind, labels[kind])
    entry_id = action.get('id')
    entry, warning = _target(conn, entry_id, context_ids)
    if warning:
        card['warnings'].append(warning)
    if kind == 'void_entry':
        if entry['reimbursed_minor'] or entry['status'] in ('reimbursed', 'voided'):
            card['errors'].append('已有报销记录或已作废的账目不能作废')
        note = str(action.get('note') or '').strip()[:500] or '通过 AI 助手作废'
        action_name = 'void'
    elif kind == 'submit_entry':
        if entry['kind'] != 'expense' or entry['status'] != 'draft':
            card['errors'].append('只有草稿支出可以记入账本')
        note = str(action.get('note') or '').strip()[:500]
        action_name = 'submit'
    else:
        if entry['kind'] != 'income' or entry['status'] != 'posted':
            card['errors'].append('只有团队在账收入可以标记已分红')
        note = str(action.get('note') or '').strip()[:500] or '通过 AI 助手标记已分红到个人账户'
        action_name = 'distribute'
    card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'action': action_name, 'note': note}
    card['summary'] = f'#{entry["id"]} {_entry_desc(entry)} · {_money(entry["amount"], entry["currency"], conf)} · {labels[kind]}'
    if note:
        card['summary'] += f' · 备注：{note[:60]}'
    return card


def _resolve_delete(conn, user, conf, action, context_ids):
    card = _card('delete_entry', '永久删除账目')
    entry, warning = _target(conn, action.get('id'), context_ids)
    if warning:
        card['warnings'].append(warning)
    if entry['status'] != 'voided':
        card['errors'].append('仅已作废账目可以永久删除；请先作废。')
    card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'description': entry['description'],
                        'amount': entry['amount'], 'currency': entry['currency']}
    card['summary'] = f'#{entry["id"]} {_entry_desc(entry)} · {_money(entry["amount"], entry["currency"], conf)} · 永久删除'
    card['warnings'].append('永久删除会清除账目、操作记录、付款记录和凭证，无法恢复。')
    return card


def _resolve_pay(conn, user, conf, action, context_ids, rates):
    card = _card('pay_entry', '登记报销')
    entry, warning = _target(conn, action.get('id'), context_ids)
    if warning:
        card['warnings'].append(warning)
    if entry['kind'] != 'expense' or entry['status'] != 'pending' or not entry['remaining_minor']:
        card['errors'].append('只有尚有剩余金额的待报销支出可以登记付款')
        card['resolved'] = {'id': entry['id'], 'version': entry['version']}
        card['summary'] = f'#{entry["id"]} {_entry_desc(entry)} · 当前状态不允许登记付款'
        return card
    requested = action.get('settlement_currency') or 'original'
    code = entry['currency'] if requested in ('original', '', None) else str(requested).upper()
    if code not in (entry['currency'], conf['reference_currency']):
        card['errors'].append('付款币种请选择记账货币或团队结算货币')
    raw_amount = action.get('amount')
    amount = _amount_value(raw_amount)
    if raw_amount not in (None, '') and amount is None:
        card['errors'].append('付款金额格式不正确')
    try:
        note = _text(action.get('note'), 500) or '通过 AI 助手登记付款'
    except PlanQuestion as exc:
        card['errors'].append(str(exc))
        note = '通过 AI 助手登记付款'
    quote = None
    if not card['errors']:
        try:
            quote = make_payment_quote(conn, user, entry['id'], code, rates)
            if amount in (None, 'FULL'):
                amount = quote['maximum_amount']
            entry_after, _, units, allocation = payment_preview(conn, user, entry['id'], amount, quote['id'])
        except HTTPException as exc:
            _error(card, exc)
    if quote and not card['errors']:
        pay_amount = format(Decimal(units) / (10 ** quote['decimals']), f'.{quote["decimals"]}f')
        allocation_text = format(Decimal(allocation) / (10 ** entry['decimals']), f'.{entry["decimals"]}f')
        remaining = entry['remaining_minor'] - allocation
        remaining_text = format(Decimal(remaining) / (10 ** entry['decimals']), f'.{entry["decimals"]}f')
        card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'amount': pay_amount,
                            'currency': quote['currency'], 'quote_id': quote['id'], 'note': note,
                            'entry_amount': allocation_text, 'entry_currency': entry['currency'],
                            'remaining_amount': remaining_text}
        card['summary'] = (f'#{entry["id"]} {_entry_desc(entry)} · 本次付款 {_money(pay_amount, quote["currency"], conf)}'
                           f' · 抵扣 {_money(allocation_text, entry["currency"], conf)} · 剩余 {_money(remaining_text, entry["currency"], conf)}')
        card['warnings'].append('请确认这笔转账已经实际完成；系统只登记，不会发起付款。')
    elif quote:
        card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'amount': amount or '',
                            'currency': quote['currency'], 'quote_id': quote['id'], 'note': note}
    else:
        card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'amount': amount or '',
                            'currency': code, 'note': note}
    return card


def _resolve_batch(conn, user, conf, action, rates, context_ids):
    card = _card('batch_pay', '批量登记报销')
    requested = action.get('settlement_currency') or 'original'
    mode = 'original' if requested in ('original', '', None) else 'settlement'
    if mode == 'settlement' and str(requested).upper() != conf['reference_currency']:
        card['errors'].append(f'批量报销的结算币种只能是 {conf["reference_currency"]}')
    payer_id, payer_name = None, ''
    if action.get('member'):
        payer_id, payer_name = _member(conn, action['member'], user)
    try:
        date_from = _strict_date(action.get('date_from'), '开始日期')
        date_to = _strict_date(action.get('date_to'), '结束日期')
    except PlanQuestion as exc:
        card['errors'].append(str(exc))
        date_from = date_to = None
    entry_ids, id_warnings = None, []
    if action.get('ids') is not None:
        entry_ids = []
        if not isinstance(action['ids'], list):
            card['errors'].append('账目编号列表格式不正确')
    if isinstance(action.get('ids'), list):
        for value in action['ids']:
            if type(value) is not int or value <= 0:
                card['errors'].append('账目编号无效')
            else:
                entry_ids.append(value)
    for entry_id in entry_ids or []:
        try:
            entry = entry_view(conn, entry_id)
        except HTTPException:
            raise PlanQuestion(f'没有找到账目 #{entry_id}，请核对编号。')
        if entry['kind'] != 'expense' or entry['status'] != 'pending':
            card['errors'].append(f'#{entry_id} 不是待报销支出，不能登记付款。')
        if entry_id not in context_ids:
            id_warnings.append(f'#{entry_id} 不在最近摘要中，请核对后再确认。')
    try:
        note = _text(action.get('note'), 500) or '通过 AI 助手批量登记付款'
    except PlanQuestion as exc:
        card['errors'].append(str(exc))
        note = '通过 AI 助手批量登记付款'
    plan = None
    reimbursement = {'mode': 'budget', 'budget': action['budget']} if action.get('budget') is not None else None
    if not card['errors']:
        try:
            plan = build_funds_plan(conn, user, rates, mode, payer_id=payer_id,
                                    entry_ids=entry_ids, date_from=date_from, date_to=date_to, note=note, reimbursement=reimbursement)
        except HTTPException as exc:
            _error(card, exc)
    if plan:
        totals = ' / '.join(_money(value, code, conf) for code, value in list(plan['totals'].items())[:6])
        card['resolved'] = {'mode': mode, 'payer_id': payer_id, 'payer_name': payer_name, 'entry_ids': entry_ids,
                            'date_from': date_from, 'date_to': date_to, 'note': note,
                            'budget': (plan.get('reimbursement') or {}).get('budget'), 'base': plan['base'],
                            'snapshot': plan['snapshot'], 'expires': plan['expires'],
                            'items': plan['items'], 'totals': plan['totals'],
                            'cost': plan['cost'], 'left': plan['left']}
        card['summary'] = f'批量登记 {len(plan["items"])} 笔报销 · 实际付款 {totals} · 折合 {_money(plan["cost"], plan["base"], conf)}'
        card['warnings'].append('请确认这些转账已经实际完成；系统只登记，不会发起付款。')
    else:
        card['resolved'] = {'mode': mode, 'payer_id': payer_id, 'payer_name': payer_name, 'entry_ids': entry_ids,
                            'date_from': date_from, 'date_to': date_to, 'note': note}
    card['warnings'].extend(id_warnings)
    return card


def _resolve_dividend(conn, user, conf, action, rates):
    card = _card('dividend', '盈余分红')
    code = str(action.get('currency') or conf['reference_currency']).upper()
    try:
        code = currency(conf, code)['code']
    except HTTPException as exc:
        _error(card, exc)
        code = conf['reference_currency']
    raw_amount = action.get('amount')
    raw_text = str(raw_amount).strip().lower() if raw_amount is not None else ''
    amount = 'all' if raw_text in ('', 'all') else _amount_value(raw_amount)
    if raw_text not in ('', 'all') and amount is None:
        card['errors'].append('分红金额格式不正确')
    try:
        note = _text(action.get('note'), 500) or '通过 AI 助手登记盈余分红'
    except PlanQuestion as exc:
        card['errors'].append(str(exc))
        note = '通过 AI 助手登记盈余分红'
    quote = preview = None
    if not card['errors']:
        try:
            quote = dividend_service.quote(conn, user, code, rates)
            preview = dividend_service.preview(conn, user, quote['id'], amount)
        except HTTPException as exc:
            _error(card, exc)
    if quote and preview:
        card['resolved'] = {'quote_id': quote['id'], 'amount': amount, 'currency': code,
                            'note': note, 'display_amount': preview['amount'],
                            'reference_currency': quote['reference_currency'],
                            'surplus': quote['surplus'], 'remaining_surplus': preview['remaining_surplus']}
        card['summary'] = (f'盈余分红 · 本次 {_money(preview["amount"], code, conf)}'
                           f' · 可分红上限 {_money(quote["maximum_amount"], code, conf)}')
        card['warnings'].append('待报销款不强制预留，分红会减少后续可报销资金；系统只登记已完成的转账，不会发起付款。')
    else:
        card['resolved'] = {'amount': 'all' if amount == 'all' else (amount or ''),
                            'currency': code, 'note': note}
    return card


# --- read-only queries ------------------------------------------------------

def _query_clauses(conn, user, conf, action, default_status=True, exclude_distributed=False):
    clauses, params = [], []
    scope = action.get('scope')
    if scope == 'mine':
        clauses.append('e.payer_id=?')
        params.append(user['id'])
    kind = action.get('filter_kind')
    if kind in ('expense', 'income'):
        clauses.append('e.kind=?')
        params.append(kind)
    status = action.get('filter_status')
    if status == 'partial':
        clauses.append("e.status='pending' AND e.reimbursed_minor>0")
    elif status in ('draft', 'pending', 'reimbursed', 'posted', 'distributed', 'voided'):
        clauses.append('e.status=?')
        params.append(status)
    elif default_status:
        clauses.append("e.status NOT IN ('draft','voided')")
        if exclude_distributed:
            clauses.append("NOT (e.kind='income' AND e.status='distributed')")
    code = action.get('filter_currency')
    if code:
        try:
            clauses.append('e.currency=?')
            params.append(currency(conf, str(code).upper())['code'])
        except HTTPException:
            raise PlanQuestion(f'没有启用币种 {code}。')
    if action.get('filter_payer'):
        payer_id, _ = _member(conn, action['filter_payer'], user)
        clauses.append('e.payer_id=?')
        params.append(payer_id)
    if action.get('text'):
        needle = '%' + str(action['text']).strip()[:60] + '%'
        clauses.append('(e.description LIKE ? OR e.note LIKE ? OR u.name LIKE ?)')
        params += [needle, needle, needle]
    date_from = _strict_date(action.get('date_from'), '开始日期')
    date_to = _strict_date(action.get('date_to'), '结束日期')
    if date_from:
        clauses.append('e.date>=?')
        params.append(date_from)
    if date_to:
        clauses.append('e.date<=?')
        params.append(date_to)
    return clauses, params


def _filtered_rows(conn, user, conf, action, default_status=True, exclude_distributed=False):
    clauses, params = _query_clauses(conn, user, conf, action, default_status, exclude_distributed)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = conn.execute('SELECT e.id FROM entries e JOIN users u ON u.id=e.payer_id' + where +
                        ' ORDER BY e.date DESC,e.id DESC', params).fetchall()
    return rows, where, params


def _list_query(conn, user, conf, action):
    rows, where, params = _filtered_rows(conn, user, conf, action)
    limit = action.get('limit')
    try:
        limit = min(max(int(limit), 1), 50) if limit is not None else 20
    except (TypeError, ValueError):
        limit = 20
    ids = [row['id'] for row in rows[:limit]]
    entries = [entry_view(conn, entry_id) for entry_id in ids]
    totals = {}
    for row in conn.execute(
            'SELECT e.currency,e.decimals,SUM(e.amount_minor) amount FROM entries e JOIN users u ON u.id=e.payer_id' +
            where + ' GROUP BY e.currency,e.decimals', params):
        totals[row['currency']] = format(Decimal(row['amount']) / (10 ** row['decimals']), f'.{row["decimals"]}f')
    return {'kind': 'entries', 'entries': entries, 'total': len(rows), 'shown': len(entries), 'totals': totals}


def _summary_query(conn, user, conf, action):
    clauses, params = _query_clauses(conn, user, conf, action, exclude_distributed=True)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = conn.execute(
        'SELECT e.kind,e.currency,e.decimals,e.amount_minor,e.payer_id,u.name payer_name '
        'FROM entries e JOIN users u ON u.id=e.payer_id' + where + ' ORDER BY e.date,e.id', params).fetchall()
    by_kind, by_member = {}, {}
    for row in rows:
        amount = Decimal(row['amount_minor']) / (10 ** row['decimals'])
        slot = by_kind.setdefault(row['kind'], {}).setdefault(
            row['currency'], {'currency': row['currency'], 'decimals': row['decimals'], 'amount': Decimal(0), 'count': 0})
        slot['amount'] += amount
        slot['count'] += 1
        person = by_member.setdefault((row['payer_id'], row['currency'], row['kind']), {
            'id': row['payer_id'], 'name': row['payer_name'], 'currency': row['currency'],
            'decimals': row['decimals'], 'kind': row['kind'], 'amount': Decimal(0), 'count': 0})
        person['amount'] += amount
        person['count'] += 1

    def format_slots(slots):
        cleaned = []
        for slot in slots:
            item = {**slot, 'amount': format(slot['amount'], f'.{slot["decimals"]}f')}
            item.pop('decimals', None)
            cleaned.append(item)
        return sorted(cleaned, key=lambda item: (item['currency'], item.get('kind', ''), item.get('name', '')))

    card = {'kind': 'summary', 'from': action.get('date_from'), 'to': action.get('date_to'), 'count': len(rows),
            'by_kind': {kind: format_slots(slots.values()) for kind, slots in by_kind.items()},
            'by_member': format_slots(by_member.values())}
    total = {}
    for kind, slots in card['by_kind'].items():
        for slot in slots:
            total.setdefault(slot['currency'], {'expense': '0', 'income': '0'})
            total[slot['currency']][kind] = slot['amount']
    card['totals'] = total
    return card


def _report_with_conn(conn, base=None, scenario=None):
    """Like fx.report, but reads the caller's connection (sees uncommitted writes)."""
    conf = settings(conn)
    base = base or conf['reference_currency']
    currency(conf, base)
    entries = [dict(row) for row in conn.execute(
        'SELECT e.*,u.name payer_name FROM entries e JOIN users u ON u.id=e.payer_id')]
    payments = [dict(row) for row in conn.execute('SELECT * FROM payments')]
    dividends = [dict(row) for row in conn.execute('SELECT * FROM dividends')]
    codes = {e['currency'] for e in entries
             if (e['kind'] == 'income' and e['status'] == 'posted') or e['status'] in ('pending', 'reimbursed')}
    codes.update(payment['currency'] for payment in payments)
    codes.update(dividend['currency'] for dividend in dividends)
    if scenario:
        cur = currency(conf, scenario['currency'])
        minor_amount(scenario['amount'], cur['decimals'])
        codes.add(cur['code'])
    return fx.calculate(entries, conf, fx.cached_rates(base, codes), scenario, payments, dividends)


def _funds_card(kind, conf, action, conn):
    base = str(action.get('base') or conf['reference_currency']).upper()
    try:
        currency(conf, base)
    except HTTPException:
        base = conf['reference_currency']
    try:
        report = _report_with_conn(conn, base)
    except Exception:
        report = None
    if not report:
        return {'kind': kind, 'complete': False, 'base': base, 'warning': '汇率或资金数据暂不可用，请稍后重试。'}
    card = {'kind': kind, 'complete': bool(report.get('complete')), 'base': base,
            'cash': report.get('cash'), 'income': report.get('income'), 'paid': report.get('paid'),
            'dividend_paid': report.get('dividend_paid'), 'dividend_available': report.get('dividend_available'),
            'due': report.get('due'), 'reserve': report.get('reserve'), 'payable': report.get('payable'),
            'remaining': report.get('remaining'), 'shortage': report.get('shortage'),
            'balances': report.get('balances', {}), 'dividend_balances': report.get('dividend_balances', {})}
    if not report.get('complete'):
        card['warning'] = report.get('fx', {}).get('warning') or '汇率不完整，跨币种金额暂不可用。'
    elif report.get('fx', {}).get('stale'):
        card['warning'] = report.get('fx', {}).get('warning') or '正在使用过期的汇率缓存，跨币种金额仅供参考。'
    if kind == 'due':
        card['members'] = report.get('members', [])
    elif kind == 'funds':
        card['members'] = report.get('members', [])
        card['fx'] = {'base': report.get('fx', {}).get('base'),
                      'source': report.get('fx', {}).get('source'),
                      'stale': report.get('fx', {}).get('stale'),
                      'missing': report.get('fx', {}).get('missing', [])}
    return card


def _dividend_query(conn, user, conf, action):
    items = dividend_service.history(conn)
    date_from = _strict_date(action.get('date_from'), '开始日期')
    date_to = _strict_date(action.get('date_to'), '结束日期')
    if date_from:
        items = [item for item in items if item['created_at'][:10] >= date_from]
    if date_to:
        items = [item for item in items if item['created_at'][:10] <= date_to]
    total = {}
    for item in items:
        slot = total.setdefault(item['currency'], {'amount': Decimal(0), 'decimals': item['decimals']})
        slot['amount'] += Decimal(item['amount'])
    return {'kind': 'dividends', 'items': items[:50], 'count': len(items),
            'total': len(items), 'totals': {code: format(slot['amount'], f'.{slot["decimals"]}f')
                                             for code, slot in total.items()}}


def _entry_query(conn, user, conf, action):
    try:
        entry = entry_view(conn, int(action.get('id')))
    except (TypeError, ValueError):
        raise PlanQuestion('请提供要查看的账目编号。')
    except HTTPException:
        raise PlanQuestion(f'没有找到账目 #{action.get("id")}。')
    events = [dict(row) for row in conn.execute(
        'SELECT e.id,e.action,e.detail,e.created_at,u.name FROM events e JOIN users u ON u.id=e.user_id WHERE entry_id=? ORDER BY e.id',
        (entry['id'],))]
    return {'kind': 'entry', 'entry': entry, 'events': events}


def run_query(conn, user, conf, action):
    query = action.get('query')
    try:
        if query == 'list_entries':
            return _list_query(conn, user, conf, action)
        if query == 'entry_detail':
            return _entry_query(conn, user, conf, action)
        if query == 'summary':
            return _summary_query(conn, user, conf, action)
        if query == 'dividends':
            return _dividend_query(conn, user, conf, action)
        if query in ('balance', 'due', 'funds'):
            return _funds_card(query, conf, action, conn)
        if query == 'simulate_income':
            base = str(action.get('base') or conf['reference_currency']).upper()
            cur = currency(conf, str(action.get('simulate_currency') or conf['reference_currency']).upper())
            amount = _amount_value(action.get('simulate_amount'))
            if amount in (None, 'FULL'):
                return {'kind': 'simulation', 'complete': False, 'base': base, 'warning': '请提供模拟进账金额。'}
            minor_amount(amount, cur['decimals'])
            report = _report_with_conn(conn, base, {'amount': amount, 'currency': cur['code']})
            return {'kind': 'simulation', 'complete': bool(report.get('complete')), 'base': report.get('base'),
                    'cash': report.get('cash'), 'due': report.get('due'), 'reserve': report.get('reserve'),
                    'payable': report.get('payable'), 'remaining': report.get('remaining'),
                    'shortage': report.get('shortage'), 'extra': report.get('extra'),
                    'dividend_paid': report.get('dividend_paid'),
                    'dividend_available': report.get('dividend_available'),
                    'members': report.get('members', []),
                    'warning': report.get('fx', {}).get('warning')
                    if (not report.get('complete') or report.get('fx', {}).get('stale')) else None}
        return {'kind': 'error', 'error': f'不支持的查询：{query}'}
    except PlanQuestion as exc:
        return {'kind': 'error', 'error': str(exc)}
    except HTTPException as exc:
        return {'kind': 'error', 'error': str(exc.detail)}


# --- plan assembly, storage and execution ----------------------------------

def resolve(conn, user, conf, raw, context, rates=None):
    actions, results = [], []
    raw_questions = raw.get('questions') if isinstance(raw.get('questions'), list) else []
    questions = list(raw_questions)
    raw_actions = raw.get('actions') if isinstance(raw.get('actions'), list) else []
    context_ids = {item['id'] for item in context.get('entries', [])}
    seen_write, targeted = False, set()

    def guard_targets(ids):
        duplicates = sorted(set(ids) & targeted)
        if duplicates:
            questions.append('账目 ' + '、'.join(f'#{entry_id}' for entry_id in duplicates) +
                             ' 在同一份清单里被多次操作，请分成两步确认。')
            return False
        targeted.update(ids)
        return True

    for action in raw_actions:
        action_type = action.get('type')
        try:
            if action_type == 'query':
                results.append({'index': len(actions) + len(results), 'type': 'query',
                                'query': action.get('query'), 'action': action,
                                'card': run_query(conn, user, conf, action)})
            elif action_type == 'create_entry':
                _append(actions, _resolve_create(conn, user, conf, action))
                seen_write = True
            elif action_type == 'edit_entry':
                ids = _ids(action)
                if not ids:
                    raise PlanQuestion('请指明要修改的账目编号。')
                if guard_targets(ids[:1]):
                    if action.get('id') is None:
                        action = {**action, 'id': ids[0]}
                    _append(actions, _resolve_edit(conn, user, conf, action, context_ids))
                    seen_write = True
            elif action_type in ('void_entry', 'submit_entry'):
                ids = _ids(action)
                if not ids:
                    raise PlanQuestion('请指明要处理的账目编号。')
                if guard_targets(ids):
                    for entry_id in ids:
                        single = dict(action)
                        single['id'] = entry_id
                        single.pop('ids', None)
                        _append(actions, _resolve_transition(conn, user, conf, single, context_ids))
                    seen_write = True
            elif action_type == 'distribute_income':
                questions.append('按收入标记分红的规则已移除。请改说「登记盈余分红」，系统会以扣除已报销和历史分红后的真实余额为限，允许待报销未结清时分红。')
            elif action_type == 'dividend':
                if seen_write:
                    questions.append('盈余分红需要基于最新账本。请先确认前面的操作，再单独登记分红。')
                else:
                    _append(actions, _resolve_dividend(conn, user, conf, action, rates))
                    seen_write = True
            elif action_type == 'pay_entry':
                ids = _ids(action)
                if not ids:
                    raise PlanQuestion('请指明要登记付款的账目编号。')
                if guard_targets(ids[:1]):
                    if action.get('id') is None:
                        action = {**action, 'id': ids[0]}
                    _append(actions, _resolve_pay(conn, user, conf, action, context_ids, rates))
                    seen_write = True
            elif action_type == 'delete_entry':
                ids = _ids(action)
                if not ids:
                    raise PlanQuestion('请指明要永久删除的账目编号。')
                if guard_targets(ids):
                    for entry_id in ids:
                        single = dict(action)
                        single['id'] = entry_id
                        single.pop('ids', None)
                        _append(actions, _resolve_delete(conn, user, conf, single, context_ids))
                    seen_write = True
            elif action_type == 'batch_pay':
                if seen_write:
                    questions.append('批量报销需要基于最新账本。请先确认前面的操作，再单独发起批量报销。')
                else:
                    _append(actions, _resolve_batch(conn, user, conf, action, rates, context_ids))
                    seen_write = True
            else:
                questions.append(f'无法识别的操作类型：{action_type}')
        except PlanQuestion as exc:
            questions.append(str(exc))
        except HTTPException as exc:
            card = _card(action_type or 'unknown', '无法执行')
            card['errors'].append(str(exc.detail))
            _append(actions, card)
            seen_write = True
    # Keep indexes stable and unique for the client.
    counter = 0
    for card in actions:
        card['index'] = counter
        counter += 1
    for result in results:
        result['index'] = counter
        counter += 1
    deduped = []
    for question in questions:
        if isinstance(question, str) and question.strip() and question.strip() not in deduped:
            deduped.append(question.strip()[:200])
    return {'reply': raw.get('reply', ''), 'questions': deduped[:5],
            'actions': actions, 'results': results,
            'executable': any(not card['errors'] for card in actions)}


def store(conn, user, plan, source):
    payload = {'reply': plan['reply'], 'questions': plan['questions'],
               'actions': plan['actions'], 'results': plan['results']}
    writable = [card for card in plan['actions'] if not card['errors']]
    if not writable:
        plan['plan_id'] = None
        plan['expires'] = None
        return plan
    plan_id = secrets.token_urlsafe(24)
    expires = int(time.time()) + PLAN_TTL
    conn.execute('INSERT INTO ai_plans(id,user_id,source,payload,created_at,expires) VALUES (?,?,?,?,?,?)',
                 (plan_id, user['id'], source, json.dumps(payload, ensure_ascii=False), config.now(), expires))
    conn.execute('DELETE FROM ai_plans WHERE expires<?', (int(time.time()) - 86400,))
    plan['plan_id'] = plan_id
    plan['expires'] = expires
    return plan


def load(conn, user, plan_id):
    row = conn.execute('SELECT * FROM ai_plans WHERE id=? AND user_id=? AND expires>?',
                       (str(plan_id or ''), user['id'], int(time.time()))).fetchone()
    if not row:
        fail('操作清单不存在或已过期，请重新描述需求。', 409)
    return row, json.loads(row['payload'])


def _create_body(resolved):
    return {key: resolved.get(key) for key in ('kind', 'amount', 'currency', 'description', 'date', 'payer_id', 'status', 'note')}


def _check_version(conn, entry_id, version):
    current = entry_view(conn, entry_id)
    if current['version'] != version:
        fail('账目已被其他操作更新，请重新描述并核对。', 409)
    return current


def execute(conn, user, plan_id, selected):
    row, payload = load(conn, user, plan_id)
    if row['executed_at'] and row['result']:
        return json.loads(row['result'])
    cards = {card['index']: card for card in payload.get('actions', [])}
    if not isinstance(selected, list) or not selected or any(type(index) is not int for index in selected) or len(set(selected)) != len(selected):
        fail('请选择要执行的操作。')
    chosen = []
    for index in selected:
        card = cards.get(index)
        if not card or card.get('errors'):
            fail('所选操作不存在或仍有错误，请重新核对。')
        chosen.append(card)
    if not chosen:
        fail('请选择要执行的操作。')
    chosen.sort(key=lambda card: card['index'])
    conf = settings(conn)
    # Validate every selected action before writing anything.
    for card in chosen:
        resolved = card['resolved']
        kind = card['type']
        try:
            if kind == 'create_entry':
                normalized_entry(conn, user, _create_body(resolved))
            elif kind == 'edit_entry':
                _check_version(conn, resolved['id'], resolved['version'])
            elif kind in ('void_entry', 'submit_entry', 'distribute_income'):
                _check_version(conn, resolved['id'], resolved['version'])
            elif kind == 'delete_entry':
                current = _check_version(conn, resolved['id'], resolved['version'])
                if current['status'] != 'voided':
                    fail('账目已不是作废状态，请重新描述。', 409)
            elif kind == 'pay_entry':
                _check_version(conn, resolved['id'], resolved['version'])
                payment_preview(conn, user, resolved['id'], resolved['amount'], resolved['quote_id'])
            elif kind == 'dividend':
                dividend_service.preview(conn, user, resolved['quote_id'], resolved['amount'])
            elif kind == 'batch_pay':
                if time.time() >= resolved['expires'] or funds_snapshot(conn) != resolved['snapshot']:
                    fail('账本已变化或确认已过期，请重新描述批量报销需求。', 409)
                for item in resolved['items']:
                    payment_preview(conn, user, item['id'], item['amount'], item['quote_id'])
        except HTTPException:
            raise
        except Exception:
            fail('操作校验失败，请重新描述。', 409)
    results, receipts = [], []
    for card in chosen:
        resolved, kind = card['resolved'], card['type']
        if kind == 'create_entry':
            entry = add_entry(conn, user, _create_body(resolved), 'ai', f'ai:{user["id"]}:{plan_id}:{card["index"]}')
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': entry['id'],
                            'description': entry['description'], 'amount': entry['amount'], 'currency': entry['currency']})
        elif kind == 'edit_entry':
            entry = edit_entry(conn, user, resolved['id'], _create_body(resolved))
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': entry['id'],
                            'description': entry['description'], 'amount': entry['amount'], 'currency': entry['currency']})
        elif kind in ('void_entry', 'submit_entry', 'distribute_income'):
            action_name = resolved['action']
            entry = transition(conn, user, resolved['id'], action_name, resolved.get('note', ''))
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': entry['id'],
                            'status': entry['status']})
        elif kind == 'delete_entry':
            receipt = delete_entry(conn, user, resolved['id'])
            if receipt:
                receipts.append(receipt)
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': resolved['id']})
        elif kind == 'pay_entry':
            entry = transition(conn, user, resolved['id'], 'pay', resolved.get('note', ''),
                               amount=resolved['amount'],
                               payment_key=f'pay:ai:{user["id"]}:{plan_id}:{card["index"]}',
                               quote_id=resolved['quote_id'])
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': entry['id'],
                            'status': entry['status'], 'reimbursed_amount': entry['reimbursed_amount'],
                            'remaining_amount': entry['remaining_amount']})
        elif kind == 'dividend':
            value = dividend_service.record(conn, user, resolved['quote_id'], resolved['amount'],
                                            resolved['note'], f'dividend:ai:{user["id"]}:{plan_id}:{card["index"]}')
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'dividend_id': value['id'],
                            'amount': value['amount'], 'currency': value['currency'], 'note': value['note']})
        elif kind == 'batch_pay':
            entry_ids = []
            for item in resolved['items']:
                entry = transition(conn, user, item['id'], 'pay', resolved['note'], amount=item['amount'],
                                   payment_key=f'pay:ai:{user["id"]}:{plan_id}:{card["index"]}:{item["id"]}',
                                   quote_id=item['quote_id'])
                entry_ids.append(entry['id'])
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_ids': entry_ids,
                            'count': len(entry_ids), 'totals': resolved.get('totals', {})})
    queries = []
    for result in payload.get('results', []):
        action = result.get('action') or {'query': result.get('query')}
        queries.append({'index': result.get('index'), 'type': 'query', 'query': action.get('query'),
                        'action': action, 'card': run_query(conn, user, conf, action)})
    executed = {'results': results, 'queries': queries, 'receipts': receipts, 'executed_at': config.now()}
    conn.execute('UPDATE ai_plans SET executed_at=?,result=? WHERE id=?',
                 (executed['executed_at'], json.dumps(executed, ensure_ascii=False), plan_id))
    return executed


def needed_rates(conn, conf, raw):
    """Fetch FX once, outside the write lock, only when the plan needs it."""
    actions = raw.get('actions') if isinstance(raw.get('actions'), list) else []
    need, query_bases, dividend_codes = False, set(), set()
    for action in actions:
        if action.get('type') == 'batch_pay':
            need = True
        elif action.get('type') == 'pay_entry' and action.get('settlement_currency') not in (None, '', 'original'):
            need = True
        elif action.get('type') == 'dividend':
            need = True
            code = str(action.get('currency') or conf['reference_currency']).upper()
            try:
                currency(conf, code)
                dividend_codes.add(code)
            except HTTPException:
                pass
        elif action.get('type') == 'query' and action.get('query') in ('balance', 'due', 'funds', 'simulate_income'):
            base = str(action.get('base') or conf['reference_currency']).upper()
            try:
                currency(conf, base)
                query_bases.add(base)
            except HTTPException:
                pass
    if not need and not query_bases:
        return None
    codes = {row['currency'] for row in conn.execute("SELECT DISTINCT currency FROM entries WHERE status IN ('pending','reimbursed','posted')")}
    codes.add(conf['reference_currency'])
    if dividend_codes:
        codes |= dividend_service.needed_codes(conn)
        codes |= dividend_codes
    # Warm query-only bases before the write lock; _report_with_conn then uses cache only.
    for base in query_bases - {conf['reference_currency']}:
        try:
            fx.rates_for(base, codes)
        except Exception:
            pass
    if not need:
        return None
    try:
        return fx.rates_for(conf['reference_currency'], codes)
    except Exception:
        return None
