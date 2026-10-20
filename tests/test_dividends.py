"""Surplus dividends preserve income, reimbursements and concurrent ledger safety."""
import json
import secrets
import time
import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from tests import test_ledger as base
from tests import test_bot_ui as ui
from app import bot, config, dividends, fx, ai
from app.db import db, init_db, settings, set_setting


class DividendTest(unittest.TestCase):
    setUp = base.LedgerTest.setUp
    tearDown = base.LedgerTest.tearDown
    post = base.LedgerTest.post
    entry = base.LedgerTest.entry
    action = base.LedgerTest.action
    new_member = base.LedgerTest.new_member
    link_bot = base.LedgerTest.link_bot
    message = staticmethod(base.LedgerTest.message)
    callback = staticmethod(base.LedgerTest.callback)
    click = ui.TelegramUiTest.click
    draft = ui.TelegramUiTest.draft
    last_message = ui.TelegramUiTest.last_message
    allow_group = ui.TelegramUiTest.allow_group
    flush_prompts = ui.TelegramUiTest.flush_prompts

    def quote(self, code='CNY'):
        response=self.post('/dividends/quote',{'currency':code})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def record(self, quote, amount='all', key=None, note='合成分红参考号'):
        return self.client.post('/api/dividends',json={'quote_id':quote['id'],'amount':amount,'note':note},
            headers={**self.headers,'Idempotency-Key':key or secrets.token_hex(16)})

    def seed_surplus(self):
        income=self.entry(kind='income',amount='1000')
        expense=self.entry(amount='400')
        self.assertEqual(self.action(expense,'pay','已报销完成').status_code,200)
        due=self.entry(amount='100')
        return income,expense,due

    def error(self):
        with db() as conn:
            row=conn.execute("SELECT payload FROM tg_outbox WHERE method='answerCallbackQuery' ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0])['text']

    def open_bot(self):
        self.link_bot();self.click(2,'dividends')
        return self.draft()['token']

    def test_partial_and_full_dividends_use_net_surplus_not_income(self):
        before=self.seed_surplus()
        before_views=self.client.get('/api/entries').json()
        quote=self.quote()
        self.assertEqual(quote['maximum_amount'],'500.00')
        response=self.record(quote,'200')
        self.assertEqual(response.status_code,200,response.text)
        r=self.client.get('/api/report').json()
        self.assertEqual((r['income'],r['paid'],r['dividend_paid'],r['cash'],r['due'],r['dividend_available']),
            ('1000.00','400.00','200.00','400.00','100.00','300.00'))
        after=self.client.get('/api/entries').json()
        self.assertEqual({e['id']:e for e in after},{e['id']:e for e in before_views})
        self.assertEqual(self.record(self.quote()).status_code,200)
        r=self.client.get('/api/report').json()
        self.assertEqual((r['cash'],r['due'],r['dividend_available']),('100.00','100.00','0.00'))
        self.assertEqual(self.action(before[2],'pay','预留款报销').status_code,200)
        self.assertEqual(self.client.get('/api/report').json()['cash'],'0.00')

    def test_no_surplus_and_simulation_cannot_create_dividends(self):
        self.entry(kind='income',amount='5');self.entry(amount='10')
        self.assertEqual(self.quote()['maximum_amount'],'0.00')
        self.assertEqual(self.record(self.quote()).status_code,400)
        self.post('/calculate',{'amount':'1000','currency':'CNY'})
        self.assertEqual(self.quote()['maximum_amount'],'0.00')
        self.assertEqual(self.client.get('/api/dividends').json(),[])

    def test_zero_negative_overpayment_and_extra_decimals_rejected(self):
        self.seed_surplus();q=self.quote()
        for amount in ('0','-1','501','200.001','NaN','1e2',{'bad':'value'}):
            self.assertEqual(self.record(q,amount).status_code,400,amount)
        self.assertEqual(self.client.get('/api/dividends').json(),[])

    def test_original_and_settlement_currency_payouts_preserve_actual_currency(self):
        self.entry(kind='income',amount='100',currency='USD')
        expense=self.entry(amount='20',currency='USD');self.action(expense,'pay','已付')
        self.entry(amount='10',currency='USD')
        q=self.quote('USD');self.assertEqual(q['maximum_amount'],'70.00')
        self.assertEqual(self.record(q,'25').status_code,200)
        r=self.client.get('/api/report').json()
        self.assertEqual((r['cash'],r['dividend_available']),('385.00','315.00'))
        self.assertEqual(r['balances'],{'USD':'55'})
        self.assertEqual(self.quote('CNY')['maximum_amount'],'315.00')
        self.assertEqual(self.record(self.quote('CNY')).status_code,200)
        rows=self.client.get('/api/dividends').json()
        self.assertEqual([d['currency'] for d in rows],['CNY','USD'])
        self.assertEqual(rows[1]['fx_rate'],'7')
        self.assertEqual(self.client.get('/api/report').json()['dividend_available'],'0.00')

    def test_all_rounds_down_at_the_payout_currency_smallest_unit(self):
        self.entry(kind='income',amount='1')
        with patch('app.fx.rates_for',return_value={'base':'CNY','rates':{'CNY':{'rate':'1','date':None},'USD':{'rate':'3','date':'2026-10-20'}},'stale':False,'missing':[]}):
            q=self.quote('USD')
        self.assertEqual(q['maximum_amount'],'0.33')
        self.assertEqual(self.record(q).status_code,200)
        with db() as conn:
            exact=dividends.rows(conn)[2][0]
            self.assertEqual(exact['amount_minor'],33)
        with patch('app.fx.rates_for',return_value={'base':'CNY','rates':{'CNY':{'rate':'1','date':None},'USD':{'rate':'3','date':'2026-10-20'}},'stale':False,'missing':[]}):
            self.assertEqual(self.client.get('/api/report').json()['cash'],'0.01')

    def test_replay_same_quote_and_conflicting_content(self):
        self.entry(kind='income',amount='100');q=self.quote();key=secrets.token_hex(16)
        first=self.record(q,'20',key)
        self.assertEqual(first.status_code,200)
        self.assertEqual(self.record(q,'20',key).json(),first.json())
        self.assertEqual(self.record(q,'21',key).status_code,409)
        self.assertEqual(self.record(q,'20',key,note='不同').status_code,409)
        self.assertEqual(self.record(q,'20').status_code,409)
        self.assertEqual(len(self.client.get('/api/dividends').json()),1)

    def test_new_expense_or_payment_invalidates_quotes_even_if_surplus_unchanged(self):
        _,_,due=self.seed_surplus();q=self.quote()
        self.action(due,'pay','已报销',amount='1')
        self.assertEqual(self.record(q,'10').status_code,409)
        q=self.quote();self.entry(amount='5')
        self.assertEqual(self.record(q,'10').status_code,409)
        self.assertEqual(self.client.get('/api/dividends').json(),[])

    def test_two_previews_cannot_spend_the_same_surplus(self):
        self.entry(kind='income',amount='100');a,b=self.quote(),self.quote()
        self.assertEqual(self.record(a,'75').status_code,200)
        self.assertEqual(self.record(b,'75').status_code,409)
        self.assertEqual(self.quote()['maximum_amount'],'25.00')

    def test_confirmation_expiry_and_active_member_ownership(self):
        self.entry(kind='income',amount='100');q=self.quote();member=self.new_member()
        with db(write=True) as conn:
            with self.assertRaises(HTTPException):dividends.record(conn,member,q['id'],'1','测试','dividend:synthetic:other')
            conn.execute('UPDATE dividend_quotes SET expires=? WHERE id=?',(int(time.time())-1,q['id']))
        self.assertEqual(self.record(q,'1').status_code,409)
        self.assertEqual(self.client.post('/api/dividends',json={}).status_code,403)
        self.assertEqual(self.post('/dividends',{}).status_code,400)

    def test_settings_protect_currency_used_only_by_dividends(self):
        self.entry(kind='income',amount='100');self.record(self.quote('HKD'),'1')
        conf=self.client.get('/api/settings').json()
        changed={**conf,'currencies':[c for c in conf['currencies'] if c['code']!='HKD']}
        self.assertEqual(self.client.put('/api/settings',json=changed,headers=self.headers).status_code,400)
        changed={**conf,'currencies':[{**c,'decimals':3} if c['code']=='HKD' else c for c in conf['currencies']]}
        self.assertEqual(self.client.put('/api/settings',json=changed,headers=self.headers).status_code,400)

    def test_unavailable_rates_and_invalid_currency_do_not_write_dividends(self):
        self.entry(kind='income',amount='100')
        with patch('app.fx.rates_for',return_value={'base':'CNY','rates':{},'stale':True,'missing':[]}):
            self.assertEqual(self.post('/dividends/quote',{'currency':'CNY'}).status_code,400)
        with patch('app.fx.rates_for',side_effect=AssertionError('Invalid currency must not request FX')):
            self.assertEqual(self.post('/dividends/quote',{'currency':[]}).status_code,400)
        self.assertEqual(self.client.get('/api/dividends').json(),[])

    def test_legacy_income_rule_removed_and_migrated_once_without_double_deduction(self):
        e=self.entry(kind='income',amount='100')
        with db(write=True) as conn:
            conn.execute("UPDATE entries SET status='distributed',review_note='旧版真实分红',updated_at='2026-10-20T08:00:00+08:00' WHERE id=?",(e['id'],))
            conn.execute("INSERT INTO events(entry_id,user_id,action,detail,created_at) VALUES (?,?,'distribute','旧版真实分红','2026-10-20T08:00:00+08:00')",(e['id'],self.user['id']))
        init_db();init_db()
        after=self.client.get('/api/entries').json()[0]
        self.assertEqual((after['amount'],after['status']),('100.00','posted'))
        self.assertEqual(len(self.client.get('/api/dividends').json()),1)
        r=self.client.get('/api/report').json()
        self.assertEqual((r['income'],r['dividend_paid'],r['cash']),('100.00','100.00','0.00'))
        self.assertEqual(self.action(after,'distribute','旧操作').status_code,409)
        response=self.client.post('/api/entries',json={'kind':'income','status':'distributed','amount':'5','currency':'CNY','description':'旧表单','date':config.today()},headers={**self.headers,'Idempotency-Key':secrets.token_hex(16)})
        self.assertEqual(response.status_code,400)
        events=self.client.get(f'/api/entries/{e["id"]}/events').json()
        self.assertTrue(any(ev['action']=='distribute' for ev in events))

    def test_export_contains_separate_dividends_and_protects_notes(self):
        self.entry(kind='income',amount='100');self.record(self.quote(),'25',note='=SUM(A1)')
        text=self.client.get('/api/export').text
        self.assertIn('dividend',text);self.assertIn("'=SUM(A1)",text)
        self.assertIn('25.00,CNY',text)

    def test_bot_partial_then_all_confirm_and_replay(self):
        self.seed_surplus();token=self.open_bot()
        self.assertIn('¥500.00 CNY',self.last_message()['text'])
        self.click(3,f'v:{token}:amount');bot.process_update(self.message(4,'200'))
        self.assertIn('¥200.00 CNY',self.last_message()['text'])
        self.assertEqual(self.client.get('/api/dividends').json(),[])
        self.click(5,f'v:{token}:note');bot.process_update(self.message(6,'已转入团队成员账户'))
        with patch('app.fx.rates_for',side_effect=AssertionError('Saved preview needs no network')):
            self.click(7,f'v:{token}:commit');self.click(8,f'v:{token}:commit')
        self.assertEqual(len(self.client.get('/api/dividends').json()),1)
        self.click(9,'dividends');token=self.draft()['token']
        self.click(10,f'v:{token}:all');self.assertIn('¥300.00 CNY',self.last_message()['text'])
        self.click(11,f'v:{token}:commit')
        r=self.client.get('/api/report').json()
        self.assertEqual((r['dividend_paid'],r['cash'],r['due']),('500.00','100.00','100.00'))
        self.assertEqual(len(self.client.get('/api/entries').json()),3)

    def test_bot_currency_choice_cross_currency_and_history(self):
        self.entry(kind='income',amount='70');token=self.open_bot()
        self.click(3,f'v:{token}:currencies:0');self.click(4,f'v:{token}:cur:USD')
        self.assertIn('$10.00 USD',self.last_message()['text'])
        self.click(5,f'v:{token}:all');self.click(6,f'v:{token}:commit');self.click(7,f'v:{token}:history:0')
        self.assertIn('D1',self.last_message()['text'])
        self.assertEqual(self.client.get('/api/report').json()['cash'],'0.00')

    def test_bot_stale_confirmation_and_other_members_cannot_commit(self):
        self.entry(kind='income',amount='100');token=self.open_bot();self.click(3,f'v:{token}:all')
        other=self.new_member()
        with db(write=True) as conn:conn.execute('INSERT INTO tg_links VALUES (?,?)',('456',other['id']))
        self.click(4,f'v:{token}:commit',sender=456);self.assertIn('不是你的当前分红',self.error())
        self.entry(amount='20')
        self.click(5,f'v:{token}:commit');self.assertIn('已变化',self.error())
        self.assertEqual(self.client.get('/api/dividends').json(),[])

    def test_bot_group_force_reply_binding_and_cancel(self):
        self.entry(kind='income',amount='100');self.open_bot();self.allow_group()
        self.click(3,'dividends',chat=-555,private=False);token=self.draft()['token']
        self.click(4,f'v:{token}:amount',chat=-555,private=False);self.flush_prompts()
        bot.process_update(self.message(5,'20',chat=-555,private=False))
        self.assertEqual(self.draft()['step'],'dividend_amount')
        msg=self.message(6,'20',chat=-555,private=False);msg['message']['reply_to_message']={'message_id':600}
        bot.process_update(msg);self.assertEqual(self.draft()['step'],'dividend_confirm')
        self.click(7,f'v:{token}:cancel',chat=-555,private=False)
        self.assertEqual(self.client.get('/api/dividends').json(),[])

    def test_dividend_invalidates_reimbursement_preview_and_reduces_next_plan(self):
        self.seed_surplus();self.link_bot();self.click(2,'funds');token=self.draft()['token']
        self.click(3,f'f:{token}:batch');self.click(4,f'f:{token}:mode:settlement')
        self.record(self.quote(),'200')
        self.click(5,f'f:{token}:commit');self.assertIn('已变化',self.error())
        self.assertEqual(self.client.get('/api/report').json()['cash'],'400.00')

    def test_history_paging_and_telegram_message_limits(self):
        self.entry(kind='income',amount='100')
        for _ in range(8):self.record(self.quote(),'1',note='🐱'*120)
        token=self.open_bot();self.click(3,f'v:{token}:history:1')
        self.assertIn('第 2/2 页',self.last_message()['text'])
        with db() as conn:
            for row in conn.execute('SELECT method,payload FROM tg_outbox'):
                data=json.loads(row['payload'])
                if row['method'] in ('sendMessage','editMessageText'):
                    self.assertLessEqual(len(data['text'].encode('utf-16-le'))//2,4096)
                    for buttons in data.get('reply_markup',{}).get('inline_keyboard',[]):
                        for button in buttons:self.assertLessEqual(len(button.get('callback_data','').encode()),64)

    def test_ai_does_not_treat_dividends_as_new_income(self):
        result={'kind':'dividend','amount':'100','currency':'CNY','description':'分红','date':config.today(),'note':''}
        response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':json.dumps(result)}}]}).encode()
        with db() as conn:conf=settings(conn)
        with patch.dict('os.environ',{'AI_API_KEY':'synthetic','AI_MODEL':'test'}),patch('urllib.request.urlopen',return_value=response):
            with self.assertRaises(HTTPException) as error:ai.parse('盈余已分红100',conf)
        self.assertIn('盈余分红',str(error.exception.detail))

    def test_concurrent_dividends_preserve_the_reimbursement_reserve(self):
        from concurrent.futures import ThreadPoolExecutor
        self.seed_surplus();quotes=[self.quote(),self.quote()]
        def pay(q):
            try:
                with db(write=True) as conn:
                    dividends.record(conn,self.user,q['id'],'400','并发合成分红','dividend:thread:'+q['id'])
                return 'saved'
            except HTTPException:
                return 'stale'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(pay,quotes))
        self.assertEqual(sorted(results),['saved','stale'])
        r=self.client.get('/api/report').json()
        self.assertEqual((r['cash'],r['due'],r['dividend_available']),('200.00','100.00','100.00'))

    def test_retired_telegram_income_actions_cannot_recreate_the_old_rule(self):
        self.entry(kind='income',amount='100');self.link_bot()
        self.click(2,'distribute:1')
        self.assertIn('旧规则已移除',self.error())
        from app.bot_ui import new_draft
        with db(write=True) as conn:
            token,_=new_draft(conn,self.user,'123','123',{'_action':'distribute','_entry_id':1,'note':'旧草稿'},'action_confirm')
        self.click(3,f'd:{token}:resume')
        self.assertIn('规则已移除',self.error())
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'posted')
        self.assertEqual(self.client.get('/api/dividends').json(),[])

    def test_jpy_smallest_unit_and_replay_after_quote_cleanup(self):
        self.entry(kind='income',amount='1')
        q=self.quote('JPY');self.assertEqual(q['maximum_amount'],'20')
        key=secrets.token_hex(16)
        first=self.record(q,'all',key);self.assertEqual(first.status_code,200)
        with db(write=True) as conn:conn.execute('DELETE FROM dividend_quotes WHERE id=?',(q['id'],))
        self.assertEqual(self.record(q,'all',key).json(),first.json())
