import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from app import config, fx, bot, ai
from app.db import db, settings, init_db
from app.domain import minor_amount, add_entry
from app.main import app, attempts


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ledger-test-', dir=os.getenv('LEDGER_TEST_TMP'))
        self.old_data, self.old_db = config.DATA, config.DB_PATH
        config.DATA = Path(self.temp.name)
        config.DB_PATH = config.DATA / 'test.sqlite3'
        self.fx_patch = patch('app.fx.fetch_rates', side_effect=lambda base, codes: {c: {'rate': {'USD':'7','EUR':'8','JPY':'0.05','HKD':'0.9','CNY':'1'}[c], 'date':'2026-10-16'} for c in codes})
        self.fx_patch.start()
        self.env_patch = patch.dict(os.environ, {'AI_API_KEY':'', 'AI_MODEL':'', 'TELEGRAM_BOT_TOKEN':''})
        self.env_patch.start()
        attempts.clear()
        self.client = TestClient(app)
        self.client.__enter__()
        response = self.client.post('/api/bootstrap', json={'username':'alice','name':'小林','password':'test-password-123','team_name':'测试团队'})
        self.assertEqual(response.status_code, 200, response.text)
        self.user = response.json()['user']
        self.csrf = response.json()['csrf']
        self.headers = {'X-CSRF-Token': self.csrf}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.fx_patch.stop()
        self.env_patch.stop()
        config.DATA, config.DB_PATH = self.old_data, self.old_db
        self.temp.cleanup()

    def post(self, path, body=None, **kwargs):
        return self.client.post('/api'+path, json=body or {}, headers=self.headers, **kwargs)

    def entry(self, **overrides):
        payload = {'kind':'expense','amount':'10.00','currency':'CNY','description':'测试支出','category':'其他支出','date':config.today()}
        payload.update(overrides)
        response = self.client.post('/api/entries', json=payload, headers={**self.headers,'Idempotency-Key':secrets.token_hex(16)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def action(self, entry, action, note='', amount=None, currency='original'):
        if action == 'pay':
            quoted = self.post('/entries/payment-quotes', {'ids':[entry['id']], 'currency':currency})
            if quoted.status_code != 200:
                return quoted
            quote = quoted.json()[0]
            return self.client.post('/api/entries/action', json={'ids':[entry['id']], 'action':'pay', 'note':note,
                'payments':[{'id':entry['id'], 'amount':amount if amount is not None else quote['maximum_amount'], 'quote_id':quote['id']}]},
                headers={**self.headers,'Idempotency-Key':secrets.token_hex(16)})
        return self.post('/entries/action', {'ids':[entry['id']], 'action':action, 'note':note})

    def new_member(self):
        response = self.post('/members', {'name':'小周','username':'bob','password':'test-password-456','role':'member'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_setup_closed_and_session(self):
        self.assertFalse(self.client.get('/api/bootstrap').json()['needs_setup'])
        self.assertEqual(self.post('/bootstrap', {'username':'xxx'}).status_code, 409)
        data = self.client.get('/api/session').json()
        self.assertEqual(data['user']['name'], '小林')
        self.assertEqual(data['csrf'], self.csrf)
        with db() as conn:
            user = conn.execute('SELECT * FROM users').fetchone()
            self.assertNotIn('test-password', user['password_hash'])

    def test_asset_urls_refresh_when_frontend_content_changes(self):
        public=Path(self.temp.name)/'public'
        public.mkdir()
        (public/'index.html').write_text('<link href="/static/style.css"><script src="/static/app.js"></script>',encoding='utf-8')
        (public/'style.css').write_text('body{}',encoding='utf-8')
        (public/'app.js').write_text('console.log(1)',encoding='utf-8')
        with patch('app.main.PUBLIC',public):
            first=self.client.get('/')
            (public/'app.js').write_text('console.log(2)',encoding='utf-8')
            second=self.client.get('/')
        scripts=[re.search(r'src="([^"]+)"',r.text).group(1) for r in (first,second)]
        styles=[re.search(r'href="([^"]+)"',r.text).group(1) for r in (first,second)]
        self.assertNotEqual(scripts[0],scripts[1])
        self.assertEqual(styles[0],styles[1])
        self.assertIn('?',scripts[1])
        self.assertEqual(second.headers['cache-control'],'no-store')

    def test_all_members_are_administrators(self):
        member = self.new_member()
        self.assertEqual(member['role'], 'owner')
        login = self.client.post('/api/login', json={'username':'bob','password':'test-password-456'})
        self.assertEqual(login.status_code, 200)
        self.headers = {'X-CSRF-Token':login.json()['csrf']}
        self.entry(kind='income', amount='50')
        e = self.entry(payer_id=self.user['id'])
        self.assertEqual(e['status'], 'pending')
        self.assertEqual(self.action(e,'pay','支付宝').status_code, 200)
        self.assertEqual(self.client.put('/api/settings',json={'team_name':'共同管理'},headers=self.headers).status_code,200)

    def test_auth_csrf_origin_and_logout(self):
        self.assertEqual(self.client.post('/api/entries',json={}).status_code,403)
        result=self.client.post('/api/login',json={'username':'alice','password':'test-password-123'},headers={'Origin':'https://evil.example'})
        self.assertEqual(result.status_code,403)
        self.assertEqual(self.post('/logout').status_code,200)
        self.assertEqual(self.client.get('/api/entries').status_code,401)

    def test_last_member_cannot_be_disabled(self):
        response=self.client.patch('/api/members/1',json={'active':False},headers=self.headers)
        self.assertEqual(response.status_code,400)

    def test_disabled_account_loses_session_and_bot_access(self):
        member=self.new_member()
        with db(write=True) as conn:
            conn.execute('INSERT INTO tg_links VALUES (?,?)',('777',member['id']))
        self.assertEqual(self.client.patch('/api/members/'+str(member['id']),json={'active':0},headers=self.headers).status_code,200)
        response=self.client.post('/api/login',json={'username':'bob','password':'test-password-456'})
        self.assertEqual(response.status_code,401)
        bot.process_update(self.message(100,'/book',sender=777))
        with db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_drafts').fetchone()[0],0)

    def test_exact_amount_precision(self):
        self.assertEqual(minor_amount('0.10',2),10)
        self.assertEqual(minor_amount('100',0),100)
        self.assertEqual(minor_amount('0.001',3),1)
        for amount,decimals in [('0',2),('-1',2),('1.001',2),('1.2',0),('1e3',2),('NaN',2),('1,000',2)]:
            with self.subTest(amount=amount),self.assertRaises(Exception):
                minor_amount(amount,decimals)

    def test_idempotent_create_and_payload_conflict(self):
        payload={'kind':'expense','amount':'0.10','currency':'CNY','description':'防重复','category':'其他支出','date':config.today()}
        headers={**self.headers,'Idempotency-Key':'same-request-12345678'}
        a=self.client.post('/api/entries',json=payload,headers=headers)
        b=self.client.post('/api/entries',json=payload,headers=headers)
        self.assertEqual(a.json()['id'],b.json()['id'])
        self.assertEqual(len(self.client.get('/api/entries').json()),1)
        payload['amount']='20'
        self.assertEqual(self.client.post('/api/entries',json=payload,headers=headers).status_code,409)

    def test_simultaneous_duplicate_create(self):
        body={'kind':'expense','amount':'12.34','currency':'CNY','description':'并发支出','category':'其他支出','date':config.today()}
        def create():
            with db(write=True) as conn:
                user=dict(conn.execute('SELECT * FROM users WHERE id=1').fetchone())
                return add_entry(conn,user,body,source_key='race-unique-key')['id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids=list(pool.map(lambda _:create(),range(4)))
        self.assertEqual(len(set(ids)),1)

    def test_batch_action_rolls_back_on_stale_row(self):
        a=self.entry()
        b=self.entry()
        quotes=self.post('/entries/payment-quotes',{'ids':[a['id'],b['id']]}).json()
        self.action(b,'void','重复')
        result=self.client.post('/api/entries/action',json={'ids':[a['id'],b['id']],'action':'pay','note':'批量付款',
            'payments':[{'id':q['entry_id'],'amount':q['maximum_amount'],'quote_id':q['id']} for q in quotes]},
            headers={**self.headers,'Idempotency-Key':'batch-rollback-123456'})
        self.assertEqual(result.status_code,409)
        entries=self.client.get('/api/entries').json()
        self.assertEqual(next(e for e in entries if e['id']==a['id'])['status'],'pending')
        self.assertEqual(len(self.client.get(f'/api/entries/{a["id"]}/events').json()),1)

    def test_reimbursement_lifecycle_requires_confirmation_note(self):
        e=self.entry(amount='37.50',status='draft')
        self.assertEqual(self.action(e,'pay','银行').status_code,409)
        self.assertEqual(self.action(e,'submit').status_code,200)
        body={**e,'amount':'38.00','note':'已补充','status':'pending'}
        self.assertEqual(self.client.put('/api/entries/'+str(e['id']),json=body,headers=self.headers).status_code,200)
        self.assertEqual(self.action(e,'pay').status_code,400)
        self.assertEqual(self.action(e,'pay','转账 TX123').status_code,200)
        self.assertEqual(self.action(e,'pay','重复付款').status_code,409)
        self.assertEqual(self.action(e,'void','不能删除付款').status_code,409)
        report=self.client.get('/api/report').json()
        self.assertEqual(report['paid'],'38.00')
        self.assertEqual(report['cash'],'-38.00')

    def test_calculation_does_not_double_count_advances(self):
        self.entry(kind='income',amount='100',currency='USD')
        self.entry(amount='300')
        paid=self.entry(amount='50',currency='USD')
        self.action(paid,'pay','已转账')
        self.entry(amount='40',currency='USD')
        self.entry(amount='9999',status='draft')
        r=self.client.get('/api/report').json()
        for key,expected in {'income':'700.00','paid':'350.00','cash':'350.00','due':'580.00','reserve':'350.00','payable':'350.00','remaining':'0.00','shortage':'230.00'}.items():
            self.assertEqual(r[key],expected,key)
        self.assertEqual(r['balances'],{'USD':'50'})

    def test_scenario_uses_current_cash_without_writing_income(self):
        self.entry(kind='income',amount='100')
        self.entry(amount='150')
        before=len(self.client.get('/api/entries').json())
        r=self.post('/calculate',{'amount':'10','currency':'USD','base':'CNY'}).json()
        self.assertEqual(r['cash'],'170.00')
        self.assertEqual(r['reserve'],'150.00')
        self.assertEqual(r['remaining'],'20.00')
        self.assertEqual(len(self.client.get('/api/entries').json()),before)

    def test_pending_reimbursements_share_funds_in_expense_date_order(self):
        member=self.new_member()
        self.entry(kind='income',amount='100')
        self.entry(amount='80',description='较新的垫付')
        yesterday=(date.fromisoformat(config.today())-timedelta(days=1)).isoformat()
        self.entry(amount='80',payer_id=member['id'],date=yesterday,description='较早的垫付')
        report=self.client.get('/api/report').json()
        self.assertEqual(report['payable'],'100.00')
        self.assertEqual(report['shortage'],'60.00')
        groups={row['id']:row for row in report['members']}
        self.assertEqual(groups[member['id']]['suggested'],'80.00')
        self.assertEqual(groups[self.user['id']]['suggested'],'20.00')
        self.assertNotIn('approved',report)

    def test_report_exposes_reference_rate_for_each_original_currency(self):
        self.entry(amount='5.98', currency='USD', description='美元垫付')
        report=self.client.get('/api/report?base=HKD').json()
        self.assertEqual(report['base'],'HKD')
        self.assertIn('USD',report['fx']['rates'])
        self.assertEqual(report['fx']['rates']['USD']['date'],'2026-10-16')
        self.assertGreater(float(report['fx']['rates']['USD']['rate']),0)

    def test_payment_plan_lists_each_project_and_suggested_amount(self):
        self.entry(kind='income',amount='100')
        old=(date.fromisoformat(config.today())-timedelta(days=1)).isoformat()
        self.entry(amount='60',date=old,description='旧项目')
        self.entry(amount='60',description='新项目')
        report=self.client.get('/api/report').json()
        self.assertEqual(len(report['members']),1)
        entries=report['members'][0]['entries']
        self.assertEqual([entry['description'] for entry in entries],['旧项目','新项目'])
        self.assertEqual(entries[0]['suggested'],'60.00')
        self.assertEqual(entries[1]['suggested'],'40.00')
        self.assertTrue(entries[0]['fully_covered'])
        self.assertFalse(entries[1]['fully_covered'])

    def test_approval_actions_are_removed(self):
        expense=self.entry()
        for action in ('approve','reject'):
            self.assertEqual(self.action(expense,action,'不需要审核').status_code,400)
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'pending')
        self.assertEqual(len(self.client.get(f'/api/entries/{expense["id"]}/events').json()),1)

    def test_legacy_status_migration_preserves_money_and_history(self):
        self.entry(kind='income',amount='100')
        legacy=[self.entry(amount=amount) for amount in ('10','20','30')]
        paid=self.entry(amount='40')
        self.assertEqual(self.action(paid,'pay','旧付款凭证').status_code,200)
        with db(write=True) as conn:
            for entry,status in zip(legacy,('submitted','approved','rejected')):
                conn.execute('UPDATE entries SET status=?,review_note=? WHERE id=?',(status,'历史处理备注',entry['id']))
            conn.execute('INSERT INTO events(entry_id,user_id,action,detail,created_at) VALUES (?,?,?,?,?)',
                         (legacy[1]['id'],self.user['id'],'approve','历史记录',config.now()))
            before=[dict(row) for row in conn.execute('SELECT * FROM entries ORDER BY id')]
            history=[dict(row) for row in conn.execute('SELECT * FROM events ORDER BY id')]
        init_db()
        with db() as conn:
            after=[dict(row) for row in conn.execute('SELECT * FROM entries ORDER BY id')]
            self.assertEqual([dict(row) for row in conn.execute('SELECT * FROM events ORDER BY id')],history)
        mapping={'submitted':'pending','approved':'pending','rejected':'draft'}
        for old,new in zip(before,after):
            self.assertEqual(new,{**old,'status':mapping.get(old['status'],old['status'])})
        init_db()
        with db() as conn:
            self.assertEqual([dict(row) for row in conn.execute('SELECT * FROM entries ORDER BY id')],after)
        report=self.client.get('/api/report').json()
        self.assertEqual(report['due'],'30.00')
        self.assertEqual(report['paid'],'40.00')
        self.assertEqual(report['payable'],'30.00')
        self.assertEqual(self.action(legacy[1],'pay','无需再次处理').status_code,200)

    def test_older_open_forms_record_pending_expenses(self):
        entry=self.entry(status='submitted')
        self.assertEqual(entry['status'],'pending')
        self.assertEqual(self.action(entry,'pay','直接报销').status_code,200)

    def test_voided_income_does_not_fund_reimbursements(self):
        e=self.entry(kind='income',amount='100')
        self.assertEqual(self.action(e,'void','重复登记').status_code,200)
        report=self.client.get('/api/report').json()
        self.assertEqual(report['income'],'0.00')
        self.assertEqual(report['cash'],'0.00')

    def test_only_voided_entries_can_be_permanently_deleted(self):
        e=self.entry(description='待删除')
        self.assertEqual(self.client.delete(f'/api/entries/{e["id"]}',headers=self.headers).status_code,409)
        self.assertEqual(self.action(e,'void','重复登记').status_code,200)
        self.assertEqual(self.action(e,'pay','不可付款').status_code,409)
        receipts=config.DATA/'receipts';receipts.mkdir(exist_ok=True)
        (receipts/'deleted-receipt.png').write_bytes(b'png')
        with db(write=True) as conn:
            conn.execute('UPDATE entries SET receipt=? WHERE id=?',('deleted-receipt.png',e['id']))
        response=self.client.delete(f'/api/entries/{e["id"]}',headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn(e['id'],[row['id'] for row in self.client.get('/api/entries').json()])
        self.assertFalse((receipts/'deleted-receipt.png').exists())
        with db() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM entries WHERE id=?',(e['id'],)).fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM events WHERE entry_id=?',(e['id'],)).fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM payments WHERE entry_id=?',(e['id'],)).fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM payment_quotes WHERE entry_id=?',(e['id'],)).fetchone())

    def test_fx_unavailable_is_explicit_and_same_currency_works(self):
        self.entry(kind='income',amount='100',currency='USD')
        with patch('app.fx.fetch_rates',side_effect=RuntimeError('offline')):
            r=self.client.get('/api/report').json()
        self.assertFalse(r['complete'])
        self.assertEqual(r['fx']['missing'],['USD'])
        self.assertNotIn('reserve',r)
        self.assertEqual(r['balances'],{'USD':'100'})
        # No API is needed to report in the same currency as all entries.
        r=self.client.get('/api/report?base=USD').json()
        self.assertTrue(r['complete'])
        self.assertEqual(r['cash'],'100.00')

    def test_stale_fx_fallback_is_labelled(self):
        self.entry(kind='income',amount='1',currency='USD')
        self.client.get('/api/report')
        with db(write=True) as conn:
            conn.execute('UPDATE fx_cache SET fetched_at=0')
        with patch('app.fx.fetch_rates',side_effect=RuntimeError('offline')):
            r=self.client.get('/api/report').json()
        self.assertTrue(r['fx']['stale'])
        self.assertIn('缓存',r['fx']['warning'])
        self.assertEqual(r['cash'],'7.00')

    def test_real_provider_response_shapes_and_reciprocal_direction(self):
        self.fx_patch.stop()
        for payload in [
            {'base':'CNY','date':'2026-10-16','rates':{'USD':0.125}},
            [{'base':'CNY','quote':'USD','date':'2026-10-16','rate':0.125}],
        ]:
            response=MagicMock()
            response.__enter__.return_value.read.return_value=json.dumps(payload).encode()
            with patch('urllib.request.urlopen',return_value=response):
                result=fx.fetch_rates('CNY',{'USD'})
            self.assertEqual(result['USD']['rate'],'8')
            self.assertEqual(result['USD']['date'],'2026-10-16')
        self.fx_patch.start()

    def test_used_currency_metadata_is_preserved(self):
        self.entry(currency='JPY',amount='500')
        conf=self.client.get('/api/settings').json()
        for c in conf['currencies']:
            if c['code']=='JPY':c['decimals']=2
        self.assertEqual(self.client.put('/api/settings',json=conf,headers=self.headers).status_code,400)
        conf['currencies']=[c for c in conf['currencies'] if c['code']!='JPY']
        self.assertEqual(self.client.put('/api/settings',json=conf,headers=self.headers).status_code,400)

    def test_csv_is_formula_safe_and_has_original_currency(self):
        self.entry(description='=SUM(1,2)',currency='USD')
        response=self.client.get('/api/export')
        self.assertEqual(response.status_code,200)
        self.assertIn("'=SUM",response.text)
        self.assertIn('USD',response.text)
        self.assertIn('原币种',response.text)

    def test_receipt_is_private_and_validated(self):
        e=self.entry()
        url=f'/entries/{e["id"]}/receipt'
        self.assertEqual(self.post(url,{'data':base64.b64encode(b'<svg></svg>').decode(),'name':'a.svg'}).status_code,400)
        self.assertEqual(self.post(url,{'data':base64.b64encode(b'%PDF-1.4 test').decode(),'name':'test.pdf'}).status_code,200)
        self.assertEqual(self.client.get('/api'+url).status_code,200)
        self.assertEqual(self.post(url,{'data':base64.b64encode(b'%PDF-1.4 updated').decode(),'name':'updated.pdf'}).status_code,200)
        self.action(e,'pay','付款后锁定凭证')
        self.assertEqual(self.post(url,{'data':base64.b64encode(b'%PDF-1.4 other').decode(),'name':'other.pdf'}).status_code,409)
        self.post('/logout')
        self.assertEqual(self.client.get('/api'+url).status_code,401)

    def test_ai_disabled_does_not_create_entries(self):
        self.assertEqual(self.post('/ai/parse',{'text':'打车35元'}).status_code,503)
        self.assertEqual(len(self.client.get('/api/entries').json()),0)

    def test_ai_produces_validated_preview_only(self):
        draft={'kind':'expense','amount':'35','currency':'CNY','description':'打车','category':'交通出行','date':config.today(),'note':''}
        with patch('app.ai.parse',return_value=draft):
            self.assertEqual(self.post('/ai/parse',{'text':'打车35元'}).status_code,200)
        self.assertEqual(len(self.client.get('/api/entries').json()),0)
        with patch('app.ai.parse',return_value={**draft,'amount':'-1'}):
            self.assertEqual(self.post('/ai/parse',{'text':'打车'}).status_code,400)

    def test_ai_json_parser_validates_upstream_response(self):
        draft={'kind':'expense','amount':'35','currency':'CNY','description':'打车','date':config.today(),'note':''}
        for amount,expected in [('35',200),(None,400),('-1',400)]:
            response=MagicMock()
            response.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':json.dumps({**draft,'amount':amount})}}]}).encode()
            with patch.dict(os.environ,{'AI_API_KEY':'test-only-not-real','AI_MODEL':'test-model'}),patch('urllib.request.urlopen',return_value=response):
                self.assertEqual(self.post('/ai/parse',{'text':'今天打车35元'}).status_code,expected)
        self.assertEqual(len(self.client.get('/api/entries').json()),0)

    @staticmethod
    def message(update_id,text,sender=123,chat=123,private=True):
        return {'update_id':update_id,'message':{'text':text,'from':{'id':sender},'chat':{'id':chat,'type':'private' if private else 'group'}}}

    @staticmethod
    def callback(update_id,data,sender=123,chat=123):
        return {'update_id':update_id,'callback_query':{'id':'callback'+str(update_id),'data':data,'from':{'id':sender},'message':{'chat':{'id':chat,'type':'private'}}}}

    def link_bot(self):
        code=self.post('/telegram/link-code').json()['code']
        bot.process_update(self.message(1,'/start '+code))
        return code

    def test_telegram_binding_is_one_time_and_private(self):
        code=self.link_bot()
        bot.process_update(self.message(2,'/start '+code,sender=456,chat=456))
        with db() as conn:
            rows=conn.execute('SELECT * FROM tg_links').fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['telegram_id'],'123')
        self.post('/telegram/unlink')
        with db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_links').fetchone()[0],0)

    def test_telegram_buttons_and_replay_cannot_duplicate_entry(self):
        self.link_bot()
        bot.process_update(self.message(2,'/book'))
        with db() as conn:token=conn.execute('SELECT token FROM tg_drafts').fetchone()[0]
        bot.process_update(self.callback(3,f'd:{token}:cur:USD'))
        bot.process_update(self.message(5,'49.50'))
        bot.process_update(self.message(6,'软件订阅'))
        self.assertEqual(len(self.client.get('/api/entries').json()),0)
        update=self.callback(7,f'd:{token}:save')
        bot.process_update(update);bot.process_update(update)
        bot.process_update(self.callback(8,f'd:{token}:save'))
        entries=self.client.get('/api/entries').json()
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['amount'],'49.50')
        self.assertEqual(entries[0]['source'],'telegram')
        self.assertEqual(entries[0]['status'],'pending')
        with db() as conn:
            messages=[json.loads(r['payload']).get('text','') for r in conn.execute('SELECT payload FROM tg_outbox')]
        self.assertTrue(any('已加入待报销' in message for message in messages))
        self.assertFalse(any('等待审核' in message for message in messages))

    def test_telegram_income_buttons(self):
        self.link_bot();bot.process_update(self.message(2,'/income'))
        with db() as conn:token=conn.execute('SELECT token FROM tg_drafts').fetchone()[0]
        bot.process_update(self.callback(3,f'd:{token}:cur:CNY'))
        bot.process_update(self.message(4,'2500'))
        bot.process_update(self.message(5,'客户尾款'))
        bot.process_update(self.callback(6,f'd:{token}:save'))
        e=self.client.get('/api/entries').json()[0]
        self.assertEqual(e['kind'],'income');self.assertEqual(e['status'],'posted')

    def test_other_telegram_user_cannot_confirm_someone_elses_draft(self):
        member=self.new_member();self.link_bot()
        with db(write=True) as conn:conn.execute('INSERT INTO tg_links VALUES (?,?)',('456',member['id']))
        bot.process_update(self.message(2,'/book'))
        with db() as conn:
            token=conn.execute('SELECT token FROM tg_drafts').fetchone()[0]
            payload={'kind':'expense','amount':'10','currency':'CNY','description':'私有草稿','category':'其他支出','date':config.today()}
        with db(write=True) as conn:conn.execute('UPDATE tg_drafts SET step=?,payload=? WHERE token=?',('confirm',json.dumps(payload),token))
        bot.process_update(self.callback(3,f'd:{token}:save',sender=456))
        self.assertEqual(len(self.client.get('/api/entries').json()),0)

    def test_unlisted_telegram_group_is_ignored(self):
        self.link_bot()
        with db() as conn:before=conn.execute('SELECT COUNT(*) FROM tg_outbox').fetchone()[0]
        bot.process_update(self.message(2,'/book',chat=-999,private=False))
        with db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_drafts').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_outbox').fetchone()[0],before)

    def test_telegram_ai_is_draft_until_confirmed(self):
        self.link_bot()
        draft={'kind':'expense','amount':'30','currency':'CNY','description':'午餐','category':'餐饮招待','date':config.today(),'note':''}
        with patch('app.ai.parse',return_value=draft):bot.process_update(self.message(2,'午餐30元'))
        self.assertEqual(len(self.client.get('/api/entries').json()),0)
        with db() as conn:token=conn.execute('SELECT token FROM tg_drafts').fetchone()[0]
        bot.process_update(self.callback(3,f'd:{token}:save'))
        self.assertEqual(len(self.client.get('/api/entries').json()),1)


if __name__=='__main__':
    unittest.main(verbosity=2)
