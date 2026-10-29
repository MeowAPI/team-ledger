"""Validate the reimbursement choices shared by reports and payment batches."""
from decimal import Decimal
from .domain import currency, fail, minor_amount


def normalize(entries, conf, base, choice=None):
    if choice is None:
        choice = {'mode': 'auto'}
    if not isinstance(choice, dict) or choice.get('mode') not in ('auto', 'budget', 'manual'):
        fail('请选择自动安排、指定报销预算或手动选择项目')
    mode = choice['mode']
    budget, ids = None, []
    if mode == 'budget':
        if choice.get('currency', base) != base:
            fail('报销预算的参考币种已变化，请重新填写预算', 409)
        decimals = currency(conf, base)['decimals']
        units = minor_amount(choice.get('budget', ''), decimals, allow_zero=True)
        budget = format(Decimal(units) / (10 ** decimals), f'.{decimals}f')
    if mode == 'manual':
        ids = choice.get('entry_ids')
        if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids):
            fail('请选择有效的待报销项目')
        pending = {e['id'] for e in entries if e['kind'] == 'expense' and e['status'] == 'pending'}
        if set(ids) - pending:
            fail('所选项目已变化或不再待报销，请刷新后重新选择', 409)
        ids = sorted(set(ids))
    return {'mode': mode, 'budget': budget, 'entry_ids': ids, 'currency': base}


def limit(choice, available, selected_due):
    budget = Decimal(choice['budget']) if choice['mode'] == 'budget' else selected_due
    return min(max(available, Decimal(0)), selected_due, budget)
