import csv
import io
import secrets
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from fastapi import HTTPException
from tests import test_ledger as base
from app.db import db, init_db, set_setting
from app.domain import transition


class PartialPaymentTest(unittest.TestCase):
    setUp = base.LedgerTest.setUp
    tearDown = base.LedgerTest.tearDown
    post = base.LedgerTest.post
    entry = base.LedgerTest.entry
    action = base.LedgerTest.action
    new_member = base.LedgerTest.new_member

    def quote(self, entry, code='original'):
        response = self.post('/entries/payment-quotes', {'ids':[entry['id']], 'currency':code})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()[0]

    def pay(self, quote, amount, key=None, note='转账凭证'):
        return self.client.post('/api/entries/action', json={'action':'pay','ids':[quote['entry_id']], 'note':note,
            'payments':[{'id':quote['entry_id'],'amount':amount,'quote_id':quote['id']}]},
            headers={**self.headers,'Idempotency-Key':key or secrets.token_hex(16)})

    def test_two_installments_keep_history_and_only_deduct_actual_payments(self):
        self.entry(kind='income',amount='1000')
        entry = self.entry(amount='100',currency='USD')
        partial = self.pay(self.quote(entry),'40').json()[0]
        self.assertEqual((partial['status'],partial['reimbursed_amount'],partial['remaining_amount']),('pending','40.00','60.00'))
        report = self.client.get('/api/report').json()
        self.assertEqual((report['paid'],report['cash'],report['due']),('280.00','720.00','420.00'))
        self.assertEqual(report['members'][0]['entries'][0]['amount'],'60.00')
        self.assertEqual(report['balances'],{'CNY':'1000','USD':'-40'})
        self.assertEqual(self.action(entry,'void','已付款不可作废').status_code,409)
        self.assertEqual(self.client.put('/api/entries/'+str(entry['id']),json={**entry,'amount':'200'},headers=self.headers).status_code,409)
        full = self.pay(self.quote(entry),'60').json()[0]
        self.assertEqual((full['status'],full['reimbursed_minor'],full['remaining_amount']),('reimbursed',10000,'0.00'))
        self.assertEqual([p['amount'] for p in full['payments']],['40.00','60.00'])
        self.assertTrue(all(p['created_at'] and p['name'] and p['note'] for p in full['payments']))
        self.assertEqual(self.client.get('/api/report').json()['paid'],'700.00')
        self.assertEqual(self.client.get('/api/report').json()['due'],'0.00')

    def test_payment_currency_can_change_between_installments(self):
        entry = self.entry(amount='100',currency='USD')
        quote = self.quote(entry,'CNY')
        self.assertEqual((quote['currency'],quote['maximum_amount']),('CNY','700.00'))
        first = self.pay(quote,'280').json()[0]
        self.assertEqual((first['reimbursed_amount'],first['remaining_amount']),('40.00','60.00'))
        self.assertEqual((first['payments'][0]['amount'],first['payments'][0]['currency'],first['payments'][0]['entry_amount']),('280.00','CNY','40.00'))
        with db(write=True) as conn:
            conn.execute('DELETE FROM fx_cache')
        with patch('app.fx.fetch_rates',return_value={'USD':{'rate':'8','date':'2026-10-17'}}):
            last = self.pay(self.quote(entry,'CNY'),'480').json()[0]
        self.assertEqual(last['status'],'reimbursed')
        self.assertEqual([p['fx_rate'] for p in last['payments']],['7','8'])
        report = self.client.get('/api/report').json()
        self.assertEqual(report['paid'],'760.00')
        self.assertEqual(report['balances'],{'CNY':'-760'})
        self.assertEqual(report['due'],'0.00')

    def test_mixed_original_and_settlement_payment_amounts(self):
        entry = self.entry(amount='100',currency='USD')
        self.pay(self.quote(entry,'CNY'),'280')
        self.pay(self.quote(entry),'10')
        report = self.client.get('/api/report').json()
        self.assertEqual((report['paid'],report['due']),('350.00','350.00'))
        self.assertEqual(report['balances'],{'CNY':'-280','USD':'-10'})

    def test_quote_is_fixed_after_rates_change_and_full_rounding_clears_dust(self):
        entry = self.entry(amount='0.03',currency='USD')
        quote = self.quote(entry,'CNY')
        with db(write=True) as conn:
            conn.execute('DELETE FROM fx_cache')
        with patch('app.fx.fetch_rates',side_effect=AssertionError('Payment must use its saved quote')):
            result = self.pay(quote,'0.21').json()[0]
        self.assertEqual((result['status'],result['remaining_minor']),('reimbursed',0))
        self.assertEqual(result['payments'][0]['fx_rate'],'7')
        with db() as conn:
            self.assertEqual(conn.execute('SELECT SUM(entry_amount_minor) FROM payments').fetchone()[0],3)

    def test_invalid_amount_expired_quote_and_currency_are_rejected(self):
        entry = self.entry(amount='100',currency='USD')
        quote = self.quote(entry,'CNY')
        for value in ('0','-1','700.01','1.001','NaN','1e2','0.01'):
            with self.subTest(value=value):
                self.assertEqual(self.pay(quote,value).status_code,400)
        self.assertEqual(self.post('/entries/payment-quotes',{'ids':[entry['id']],'currency':'EUR'}).status_code,400)
        with db(write=True) as conn:
            conn.execute('UPDATE payment_quotes SET expires=? WHERE id=?',(int(time.time())-1,quote['id']))
        self.assertEqual(self.pay(quote,'1').status_code,409)
        self.assertEqual(self.client.get('/api/entries').json()[0]['reimbursed_minor'],0)

    def test_payment_replay_and_payload_conflict_are_safe(self):
        entry = self.entry(amount='100')
        quote = self.quote(entry)
        key = 'replay-payment-123456789'
        for _ in range(2):
            self.assertEqual(self.pay(quote,'40',key).status_code,200)
        self.assertEqual(self.pay(quote,'41',key).status_code,409)
        self.assertEqual(self.pay(quote,'40',key,note='不同备注').status_code,409)
        self.assertEqual(self.pay(quote,'40').status_code,409)
        result = self.client.get('/api/entries').json()[0]
        self.assertEqual((result['reimbursed_amount'],len(result['payments'])),('40.00',1))

    def test_concurrent_different_requests_cannot_pay_the_same_stale_balance(self):
        entry = self.entry(amount='100')
        quotes = [self.quote(entry),self.quote(entry)]
        def pay(quote):
            try:
                with db(write=True) as conn:
                    user = dict(conn.execute('SELECT * FROM users WHERE id=1').fetchone())
                    transition(conn,user,entry['id'],'pay','并发付款',amount='60',payment_key=secrets.token_hex(16),quote_id=quote['id'])
                return 200
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(pay,quotes)),[200,409])
        self.assertEqual(self.client.get('/api/entries').json()[0]['reimbursed_amount'],'60.00')

    def test_other_member_cannot_reuse_payment_quote(self):
        entry = self.entry()
        quote = self.quote(entry)
        self.new_member()
        login = self.client.post('/api/login',json={'username':'bob','password':'test-password-456'}).json()
        self.headers = {'X-CSRF-Token':login['csrf']}
        self.assertEqual(self.pay(quote,'5').status_code,409)

    def test_missing_exchange_rate_still_allows_original_currency(self):
        entry = self.entry(currency='USD')
        with patch('app.fx.fetch_rates',side_effect=OSError('offline')):
            self.assertEqual(self.post('/entries/payment-quotes',{'ids':[entry['id']],'currency':'CNY'}).status_code,400)
            self.assertEqual(self.pay(self.quote(entry),'5').status_code,200)

    def test_settlement_only_currency_metadata_is_locked_after_payment(self):
        entry = self.entry(amount='100',currency='USD')
        with db(write=True) as conn:
            set_setting(conn,'reference_currency','HKD')
        self.pay(self.quote(entry,'HKD'),'70')
        conf = self.client.get('/api/settings').json()
        # HKD has a payment but no original ledger entry.
        conf['currencies'] = [{**c,'decimals':0} if c['code']=='HKD' else c for c in conf['currencies']]
        self.assertEqual(self.client.put('/api/settings',json=conf,headers=self.headers).status_code,400)
        conf['currencies'] = [c for c in conf['currencies'] if c['code']!='HKD']
        conf['reference_currency'] = 'CNY'
        self.assertEqual(self.client.put('/api/settings',json=conf,headers=self.headers).status_code,400)

    def test_mixed_currency_batch_records_each_actual_amount(self):
        a,b = self.entry(amount='100',currency='USD'),self.entry(amount='50')
        qa,qb = self.quote(a,'CNY'),self.quote(b)
        body = {'action':'pay','ids':[a['id'],b['id']], 'note':'一批实际转账', 'payments':[
            {'id':a['id'],'amount':'280','quote_id':qa['id']},
            {'id':b['id'],'amount':'10','quote_id':qb['id']}]}
        headers = {**self.headers,'Idempotency-Key':'mixed-batch-123456789'}
        for _ in range(2):
            response = self.client.post('/api/entries/action',json=body,headers=headers)
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([p['reimbursed_amount'] for p in response.json()],['40.00','10.00'])
        report = self.client.get('/api/report').json()
        self.assertEqual((report['paid'],report['due']),('290.00','460.00'))

    def test_legacy_full_payments_migrate_once_preserving_records_and_dates(self):
        entry = self.entry(amount='23.45')
        stamp = '2026-09-01T12:00:00+08:00'
        with db(write=True) as conn:
            conn.execute("UPDATE entries SET status='reimbursed',paid_at=?,review_note='旧转账' WHERE id=?",(stamp,entry['id']))
            conn.execute('ALTER TABLE entries DROP COLUMN reimbursed_minor')
        init_db(); init_db()
        result = self.client.get('/api/entries').json()[0]
        self.assertEqual((result['reimbursed_amount'],result['remaining_amount']),('23.45','0.00'))
        self.assertEqual(len(result['payments']),1)
        self.assertEqual((result['payments'][0]['created_at'],result['payments'][0]['note']),(stamp,'旧转账'))
        self.assertEqual(self.client.get('/api/report').json()['paid'],'23.45')

    def test_export_includes_paid_and_remaining_in_original_currency(self):
        entry = self.entry(amount='100',currency='USD')
        self.pay(self.quote(entry,'CNY'),'280')
        rows = list(csv.reader(io.StringIO(self.client.get('/api/export').text.lstrip('\ufeff'))))
        record = dict(zip(rows[0],rows[1]))
        self.assertEqual(record['累计已报销（原币种）'],'40.00')
        self.assertEqual(record['剩余待报销（原币种）'],'60.00')


if __name__ == '__main__':
    unittest.main()
