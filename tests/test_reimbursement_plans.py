"""Budget and project choices affect planning, never the stored ledger."""
import unittest
from decimal import Decimal
from unittest.mock import patch
from tests import test_ledger as base
from app.db import db, settings, set_setting
from app import fx
from app.funds_plan import build_plan


class ReimbursementPlanTest(unittest.TestCase):
    setUp = base.LedgerTest.setUp
    tearDown = base.LedgerTest.tearDown
    post = base.LedgerTest.post
    entry = base.LedgerTest.entry
    action = base.LedgerTest.action

    def calculate(self, choice=None, **extra):
        response = self.post('/calculate', {'reimbursement': choice, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def projects(self, report):
        return {e['id']: e for m in report['members'] for e in m['entries']}

    def test_budget_limits_oldest_first_and_preserves_cash_and_unpaid_balances(self):
        self.entry(kind='income', amount='200')
        first = self.entry(amount='80', date='2026-10-01')
        second = self.entry(amount='100', date='2026-10-02')
        before = self.client.get('/api/entries').json()
        report = self.calculate({'mode': 'budget', 'budget': '100'})
        self.assertEqual((report['cash'], report['reserve'], report['remaining'], report['unplanned']),
                         ('200.00', '100.00', '100.00', '80.00'))
        projects = self.projects(report)
        self.assertEqual(projects[first['id']]['suggested'], '80.00')
        self.assertEqual(projects[second['id']]['suggested'], '20.00')
        self.assertFalse(projects[second['id']]['fully_covered'])
        self.assertEqual(self.client.get('/api/entries').json(), before)
        self.assertEqual(self.client.get('/api/dividends').json(), [])

    def test_budget_zero_and_caps_at_actual_cash_or_due(self):
        self.entry(kind='income', amount='50')
        self.entry(amount='80')
        for budget, reserve, remaining in [('0', '0.00', '50.00'), ('0.00', '0.00', '50.00'),
                                            ('10.25', '10.25', '39.75'), ('1000', '50.00', '0.00')]:
            with self.subTest(budget=budget):
                report = self.calculate({'mode': 'budget', 'budget': budget})
                self.assertEqual((report['reserve'], report['remaining']), (reserve, remaining))
        self.entry(kind='income', amount='100')
        report = self.calculate({'mode': 'budget', 'budget': '1000'})
        self.assertEqual((report['reserve'], report['remaining']), ('80.00', '70.00'))

    def test_manual_selection_skips_older_unselected_expenses_and_accepts_empty(self):
        self.entry(kind='income', amount='60')
        first = self.entry(amount='50', date='2026-10-01')
        second = self.entry(amount='80', date='2026-10-02')
        report = self.calculate({'mode': 'manual', 'entry_ids': [second['id'], second['id']]})
        self.assertEqual((report['selected_due'], report['reserve'], report['unplanned']), ('80.00', '60.00', '70.00'))
        projects = self.projects(report)
        self.assertFalse(projects[first['id']]['selected'])
        self.assertEqual(projects[first['id']]['suggested'], '0.00')
        self.assertEqual(projects[second['id']]['suggested'], '60.00')
        empty = self.calculate({'mode': 'manual', 'entry_ids': []})
        self.assertEqual((empty['reserve'], empty['remaining'], empty['unplanned']), ('0.00', '60.00', '130.00'))
        self.assertTrue(all(e['suggested'] == '0.00' for e in self.projects(empty).values()))

    def test_partial_reimbursements_and_mixed_currencies_use_remaining_only(self):
        self.entry(kind='income', amount='200')
        expense = self.entry(currency='USD', amount='20')
        self.assertEqual(self.action(expense, 'pay', 'first installment', amount='5').status_code, 200)
        report = self.calculate({'mode': 'manual', 'entry_ids': [expense['id']]})
        self.assertEqual((report['cash'], report['selected_due'], report['reserve'], report['remaining']),
                         ('165.00', '105.00', '105.00', '60.00'))
        project = self.projects(report)[expense['id']]
        self.assertEqual((project['amount'], project['reimbursed_amount']), ('15.00', '5.00'))

    def test_simulated_income_funds_planning_but_not_real_dividends(self):
        self.entry(kind='income', amount='10')
        self.entry(amount='100')
        report = self.calculate({'mode': 'budget', 'budget': '50'}, amount='10', currency='USD')
        self.assertEqual((report['cash'], report['actual_cash'], report['reserve'], report['remaining'], report['dividend_available']),
                         ('80.00', '10.00', '50.00', '30.00', '10.00'))
        quote = self.post('/dividends/quote', {'currency': 'CNY'}).json()
        self.assertEqual(quote['maximum_amount'], '10.00')
        self.assertEqual(self.client.get('/api/report').json()['cash'], '10.00')

    def test_invalid_choices_rejected_without_fetching_rates(self):
        income = self.entry(kind='income', currency='USD', amount='10')
        draft = self.entry(status='draft')
        voided = self.entry()
        self.action(voided, 'void', 'duplicate')
        choices = [[], {}, {'mode': 'unknown'}, {'mode': 'manual'}, {'mode': 'manual', 'entry_ids': None},
                   {'mode': 'manual', 'entry_ids': ['1']}, {'mode': 'manual', 'entry_ids': [True]},
                   {'mode': 'manual', 'entry_ids': [99999]},
                   *({'mode': 'manual', 'entry_ids': [e['id']]} for e in (income, draft, voided)),
                   *({'mode': 'budget', 'budget': b} for b in ('', '-1', '1.001', 'NaN', '1e2', [], None))]
        with patch('app.fx.rates_for', side_effect=AssertionError('Invalid choices must not request FX')):
            for choice in choices:
                with self.subTest(choice=choice):
                    self.assertIn(self.post('/calculate', {'reimbursement': choice}).status_code, (400, 409))

    def test_settled_or_deleted_selection_requires_refresh(self):
        self.entry(kind='income', amount='100')
        expense = self.entry(amount='10')
        self.assertEqual(self.action(expense, 'pay', 'paid').status_code, 200)
        self.assertEqual(self.post('/calculate', {'reimbursement': {'mode': 'manual', 'entry_ids': [expense['id']]}}).status_code, 409)

    def test_budget_currency_change_requires_reentry(self):
        self.entry(kind='income', amount='100')
        self.entry(amount='20')
        response = self.post('/calculate', {'base': 'CNY', 'reimbursement': {'mode': 'budget', 'budget': '10', 'currency': 'HKD'}})
        self.assertEqual(response.status_code, 409)

    def test_budget_respects_selected_reference_currency_precision(self):
        self.entry(kind='income', amount='100')
        self.entry(amount='80')
        with patch('app.fx.fetch_rates', return_value={'CNY': {'rate': '20', 'date': '2026-10-28'}}):
            report = self.calculate({'mode': 'budget', 'budget': '100'}, base='JPY')
        self.assertEqual((report['reserve'], report['remaining']), ('100', '1900'))
        self.assertEqual(self.post('/calculate', {'base': 'JPY', 'reimbursement': {'mode': 'budget', 'budget': '1.5'}}).status_code, 400)

    def test_actual_batch_plan_cannot_exceed_budget_and_can_target_later_project(self):
        self.entry(kind='income', amount='100')
        first = self.entry(amount='70', date='2026-10-01')
        second = self.entry(amount='80', date='2026-10-02')
        with db(write=True) as conn:
            rates = fx.cached_rates(settings(conn)['reference_currency'], {'CNY'})
            budget = build_plan(conn, self.user, rates, 'settlement', reimbursement={'mode': 'budget', 'budget': '25'})
            manual = build_plan(conn, self.user, rates, 'original', reimbursement={'mode': 'manual', 'entry_ids': [second['id']]})
        self.assertEqual([(e['id'], e['amount']) for e in budget['items']], [(first['id'], '25.00')])
        self.assertEqual([(e['id'], e['amount']) for e in manual['items']], [(second['id'], '80.00')])
        self.assertEqual(Decimal(manual['left']), Decimal('20.00'))

    def test_budgeted_cross_currency_batch_rounds_down_without_overspending(self):
        with db(write=True) as conn:
            set_setting(conn, 'reference_currency', 'HKD')
        with patch('app.fx.fetch_rates', return_value={'USD': {'rate': '7.8', 'date': '2026-10-28'}}):
            self.entry(kind='income', currency='HKD', amount='100')
            self.entry(currency='USD', amount='20')
            self.client.get('/api/report')
        with db(write=True) as conn:
            rates = fx.cached_rates('HKD', {'USD'})
            plan = build_plan(conn, self.user, rates, 'original', reimbursement={'mode': 'budget', 'budget': '20'})
        self.assertEqual(plan['items'][0]['amount'], '2.56')
        self.assertEqual(plan['cost'], '19.968')
