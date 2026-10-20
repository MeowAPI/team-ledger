"""Funds planning and dividend income tests, using isolated ledgers and mocked Telegram/FX."""
import json
import time
import unittest
from decimal import Decimal
from unittest.mock import patch
from fastapi import HTTPException
from tests import test_ledger as base
from tests import test_bot_ui as ui
from app import bot, bot_funds, config
from app.db import db, set_setting
from app.domain import transition


class FundsTest(unittest.TestCase):
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

    def open_funds(self):
        self.link_bot()
        self.click(2, 'funds')
        return self.draft()['token']

    def prepare(self, token, mode='settlement', start=10):
        self.click(start, f'f:{token}:batch')
        self.click(start+1, f'f:{token}:mode:{mode}')
        return json.loads(self.draft()['payload']).get('_plan')

    def payment_count(self):
        with db() as conn:
            return conn.execute('SELECT COUNT(*) FROM payments').fetchone()[0]

    def error(self):
        with db() as conn:
            row = conn.execute("SELECT payload FROM tg_outbox WHERE method='answerCallbackQuery' ORDER BY id DESC LIMIT 1").fetchone()
            return json.loads(row[0])['text']

    def test_simulation_is_not_income_and_actual_batch_uses_actual_funds(self):
        self.entry(kind='income',amount='5')
        self.entry(amount='30',description='垫付项目')
        token=self.open_funds()
        self.click(3,f'f:{token}:calc'); self.click(4,f'f:{token}:cur:USD')
        with patch('app.ai.parse', side_effect=AssertionError('Calculator must not invoke AI')):
            bot.process_update(self.message(5,'10'))
        self.assertIn('¥75.00 CNY',self.last_message()['text'])
        self.assertIn('仅作试算',self.last_message()['text'])
        self.assertEqual(len(self.client.get('/api/entries').json()),2)
        plan=self.prepare(token)
        self.assertEqual(plan['totals'],{'CNY':'5.00'})
        self.assertEqual(plan['items'][0]['remaining'],'25.00')
        self.assertEqual(self.payment_count(),0)
        self.click(12,f'f:{token}:commit')
        self.assertEqual(self.payment_count(),1)
        self.assertEqual(self.client.get('/api/report').json()['cash'],'0.00')

    def test_simulated_income_requires_explicit_entry_preview_and_save(self):
        token=self.open_funds()
        self.click(3,f'f:{token}:calc');self.click(4,f'f:{token}:cur:CNY')
        bot.process_update(self.message(5,'100'))
        self.click(6,f'f:{token}:income')
        self.assertEqual(self.draft()['step'],'confirm')
        self.assertEqual(self.client.get('/api/entries').json(),[])
        new_token=self.draft()['token'];self.click(7,f'd:{new_token}:save')
        self.assertEqual(self.client.get('/api/report').json()['income'],'100.00')

    def test_settlement_mixed_currency_partial_and_replay(self):
        with db(write=True) as conn:
            set_setting(conn,'reference_currency','HKD')
        with patch('app.fx.fetch_rates',return_value={'USD':{'rate':'7.8','date':'2026-10-19'}}):
            self.entry(kind='income',currency='HKD',amount='100')
            first=self.entry(currency='USD',amount='10',description='早期项目',date='2026-10-01')
            second=self.entry(currency='HKD',amount='50',description='后续项目',date='2026-10-02')
            token=self.open_funds();plan=self.prepare(token)
        self.assertEqual([i['id'] for i in plan['items']],[first['id'],second['id']])
        self.assertEqual([i['amount'] for i in plan['items']],['78.00','22.00'])
        self.assertEqual([i['remaining'] for i in plan['items']],['0.00','28.00'])
        with patch('app.fx.rates_for',side_effect=AssertionError('Confirmation uses saved rates')):
            self.click(12,f'f:{token}:commit');self.click(13,f'f:{token}:commit')
            self.click(13,f'f:{token}:commit')
        self.assertEqual(self.payment_count(),2)
        self.assertIn('已登记报销 · 2 笔',self.last_message()['text'])
        rows={e['id']:e for e in self.client.get('/api/entries').json()}
        self.assertEqual(rows[second['id']]['status'],'pending')
        self.assertEqual(rows[second['id']]['reimbursed_amount'],'22.00')
        self.assertEqual(rows[first['id']]['payments'][0]['currency'],'HKD')

    def test_original_currency_batch_preserves_actual_payment_currencies(self):
        self.entry(kind='income',amount='95')
        self.entry(currency='USD',amount='10',date='2026-10-01')
        self.entry(amount='40',date='2026-10-02')
        token=self.open_funds();plan=self.prepare(token,'original')
        self.assertEqual(plan['totals'],{'USD':'10.00','CNY':'25.00'})
        self.click(12,f'f:{token}:commit')
        report=self.client.get('/api/report').json()
        self.assertEqual((report['cash'],report['due']),('0.00','15.00'))
        self.assertEqual(report['balances'],{'CNY':'70','USD':'-10'})

    def test_rounding_does_not_overspend_or_accidentally_settle_remainder(self):
        with patch('app.fx.fetch_rates',return_value={'USD':{'rate':'100','date':'2026-10-19'}}):
            self.entry(kind='income',amount='1.75')
            self.entry(currency='USD',amount='0.02')
            token=self.open_funds();plan=self.prepare(token)
        self.assertEqual(plan['items'][0]['amount'],'1.49')
        self.assertEqual(plan['items'][0]['credited'],'0.01')
        self.click(12,f'f:{token}:commit')
        self.assertEqual(self.payment_count(),1)

    def test_minimum_unit_dust_is_not_recorded_as_a_payment(self):
        with patch('app.fx.fetch_rates',return_value={'USD':{'rate':'100','date':'2026-10-19'}}):
            self.entry(kind='income',amount='0.10');self.entry(currency='USD',amount='1')
            token=self.open_funds();self.assertIsNone(self.prepare(token))
        self.assertIn('不足',self.error())
        self.assertEqual(self.payment_count(),0)

    def test_already_partial_expense_only_pays_remaining(self):
        self.entry(kind='income',amount='100')
        e=self.entry(amount='50')
        self.assertEqual(self.action(e,'pay','先付',amount='20').status_code,200)
        token=self.open_funds();plan=self.prepare(token)
        self.assertEqual(plan['items'][0]['amount'],'30.00')
        self.click(12,f'f:{token}:commit')
        self.assertEqual(self.payment_count(),2)

    def test_concurrent_ledger_changes_reject_entire_batch(self):
        self.entry(kind='income',amount='50');self.entry(amount='10');second=self.entry(amount='20')
        token=self.open_funds();self.prepare(token)
        self.action(second,'pay','其他成员先付',amount='1')
        self.click(12,f'f:{token}:commit')
        self.assertIn('账本或结算币种已变化',self.error())
        self.assertEqual(self.payment_count(),1)

    def test_late_income_and_new_older_project_invalidate_preview(self):
        self.entry(kind='income',amount='30');self.entry(amount='30')
        token=self.open_funds();self.prepare(token)
        self.entry(kind='income',amount='50')
        self.entry(amount='10',date='2026-01-01')
        self.click(12,f'f:{token}:commit')
        self.assertIn('已变化',self.error());self.assertEqual(self.payment_count(),0)

    def test_all_payments_rollback_if_later_item_fails(self):
        self.entry(kind='income',amount='50');self.entry(amount='10');second=self.entry(amount='20')
        token=self.open_funds();self.prepare(token)
        def pay(conn,user,entry_id,*args,**kwargs):
            if entry_id==second['id']:raise HTTPException(409,'合成冲突')
            return transition(conn,user,entry_id,*args,**kwargs)
        with patch('app.bot_funds.transition',side_effect=pay):self.click(12,f'f:{token}:commit')
        self.assertEqual(self.payment_count(),0)
        self.assertEqual(self.draft()['step'],'funds_confirm')

    def test_expired_quote_rejects_entire_batch(self):
        self.entry(kind='income',amount='50');self.entry(amount='10');self.entry(amount='20')
        token=self.open_funds();plan=self.prepare(token)
        with db(write=True) as conn:
            conn.execute('UPDATE payment_quotes SET expires=? WHERE id=?',(int(time.time())-1,plan['items'][1]['quote_id']))
        self.click(12,f'f:{token}:commit')
        self.assertIn('过期',self.error());self.assertEqual(self.payment_count(),0)

    def test_owner_chat_checks_and_group_prompt(self):
        token=self.open_funds();self.allow_group()
        self.click(3,f'f:{token}:batch',chat=-555,private=False)
        self.assertIn('不是你的当前计算',self.error())
        self.click(4,'funds',chat=-555,private=False);token=self.draft()['token']
        self.click(5,f'f:{token}:calc',chat=-555,private=False)
        self.click(6,f'f:{token}:cur:CNY',chat=-555,private=False)
        self.flush_prompts()
        msg=self.message(7,'100',chat=-555,private=False)
        bot.process_update(msg);self.assertEqual(self.draft()['step'],'funds_amount')
        msg=self.message(8,'100',chat=-555,private=False);msg['message']['reply_to_message']={'message_id':600}
        bot.process_update(msg);self.assertEqual(self.draft()['step'],'funds_view')
        self.assertEqual(len(self.client.get('/api/entries').json()),0)
        with db() as conn:
            row=conn.execute('SELECT payload FROM tg_outbox ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual(json.loads(row[0]).get('message_thread_id'),None)

    def test_paginated_plans_receipts_and_message_limits(self):
        self.entry(kind='income',amount='1000')
        for i in range(13):self.entry(amount='1',description=f'{i}'+('🐱' * 55))
        token=self.open_funds()
        self.assertIn('第 1/3 页',self.last_message()['text'])
        self.click(3,f'f:{token}:view:2');self.assertIn('第 3/3 页',self.last_message()['text'])
        self.prepare(token);self.click(12,f'f:{token}:plan:2')
        self.click(13,f'f:{token}:note');bot.process_update(self.message(14,'合成付款参考号'))
        self.click(15,f'f:{token}:commit');self.click(16,f'f:{token}:receipt:2')
        self.assertIn('合成付款参考号',self.last_message()['text'])
        self.assertEqual(self.payment_count(),13)
        with db() as conn:
            for row in conn.execute('SELECT method,payload FROM tg_outbox'):
                value=json.loads(row['payload'])
                if row['method'] in ('sendMessage','editMessageText'):
                    self.assertLessEqual(len(value['text'].encode('utf-16-le'))//2,4096)
                    for buttons in value.get('reply_markup',{}).get('inline_keyboard',[]):
                        for b in buttons:self.assertLessEqual(len(b.get('callback_data','').encode()),64)

    def test_dividend_income_is_excluded_from_funds_and_plans(self):
        self.entry(kind='income',amount='5')
        distributed=self.entry(kind='income',amount='1000',currency='EUR',status='distributed')
        self.entry(amount='20')
        with patch('app.fx.fetch_rates',side_effect=AssertionError('EUR dividend must not need FX')):
            token=self.open_funds();plan=self.prepare(token)
        self.assertEqual(plan['totals'],{'CNY':'5.00'})
        self.assertEqual(distributed['status'],'distributed')
        r=self.client.get('/api/report').json()
        self.assertEqual((r['income'],r['cash'],r['shortage']),('5.00','5.00','15.00'))
        self.assertEqual(r['distributed_balances'],{'EUR':'1000'})

    def test_marking_distributed_retains_history_and_invalidates_batch(self):
        income=self.entry(kind='income',amount='100');self.entry(amount='20')
        token=self.open_funds();self.prepare(token)
        resp=self.action(income,'distribute','已转入个人账户')
        self.assertEqual(resp.status_code,200)
        self.assertEqual(self.action(income,'distribute','重复').status_code,409)
        self.click(12,f'f:{token}:commit');self.assertEqual(self.payment_count(),0)
        events=self.client.get(f'/api/entries/{income["id"]}/events').json()
        self.assertTrue(any(e['action']=='distribute' for e in events))
        report=self.client.get('/api/report').json()
        self.assertEqual(report['cash'],'0.00')
        self.assertEqual(len(self.client.get('/api/entries').json()),2)

    def test_telegram_income_toggle_and_existing_income_dividend_confirmation(self):
        self.link_bot();self.click(2,'newincome');token=self.draft()['token']
        self.click(3,f'd:{token}:cur:CNY');bot.process_update(self.message(4,'100'))
        bot.process_update(self.message(5,'已分红收入'))
        self.click(6,f'd:{token}:incomestate')
        self.assertIn('不计团队余额',self.last_message()['text'])
        self.click(7,f'd:{token}:save')
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'distributed')
        existing=self.entry(kind='income',amount='50')
        self.click(8,f'distribute:{existing["id"]}');token=self.draft()['token']
        bot.process_update(self.message(9,'已分红到个人账户'))
        self.assertIn('标记已分红',self.last_message()['text'])
        self.assertEqual(self.client.get('/api/report').json()['cash'],'50.00')
        self.click(10,f'd:{token}:commit')
        self.assertEqual(self.client.get('/api/report').json()['cash'],'0.00')
        bot.process_update(self.message(11,'/today'))
        self.assertNotIn('¥150.00',self.last_message()['text'])

    def test_expense_cannot_use_distributed_status_or_transition(self):
        e=self.entry(amount='10')
        self.assertEqual(self.action(e,'distribute','测试').status_code,409)
        self.assertEqual(self.post('/entries',{'kind':'expense','amount':'10','currency':'CNY','description':'不应入账','date':config.today(),'status':'distributed'}).status_code,400)

    def test_ai_dividend_extraction_remains_preview_only(self):
        self.link_bot()
        with patch('app.ai.parse',return_value={'kind':'income','amount':'100','currency':'CNY','description':'已分红收入','date':config.today(),'note':'','status':'distributed'}):
            bot.process_update(self.message(2,'收入100元已经分红到个人账户'))
        self.assertIn('不计团队余额',self.last_message()['text'])
        self.assertEqual(self.client.get('/api/entries').json(),[])

    def test_reopening_funds_after_confirmation_or_receipt_fetches_current_report(self):
        self.entry(kind='income',amount='50');self.entry(amount='10')
        token=self.open_funds();self.prepare(token)
        self.click(12,'funds')
        self.assertIn('当前可用资金',self.last_message()['text'])
        token=self.draft()['token'];self.prepare(token,start=20);self.click(22,f'f:{token}:commit')
        bot.process_update(self.message(23,'/funds'))
        self.assertIn('¥40.00 CNY',self.last_message()['text'])
        self.assertIn('当前可用资金',self.last_message()['text'])

    def test_another_linked_member_cannot_confirm_someone_elses_plan(self):
        other=self.new_member()
        self.entry(kind='income',amount='50');self.entry(amount='10')
        token=self.open_funds();self.prepare(token)
        with db(write=True) as conn:conn.execute('INSERT INTO tg_links VALUES (?,?)',('456',other['id']))
        self.click(12,f'f:{token}:commit',sender=456)
        self.assertIn('不是你的当前计算',self.error())
        self.assertEqual(self.payment_count(),0)
        self.click(13,f'f:{token}:commit');self.assertEqual(self.payment_count(),1)

    def test_ai_parser_maps_explicit_completed_dividends_to_distributed_status(self):
        from app import ai
        from app.db import settings
        from unittest.mock import MagicMock
        result={'kind':'income','amount':'10','currency':'CNY','description':'分红收入','date':config.today(),'note':'','income_distributed':True}
        response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':json.dumps(result)}}]}).encode()
        with db() as conn:conf=settings(conn)
        with patch.dict('os.environ',{'AI_API_KEY':'synthetic-not-real','AI_MODEL':'test'}),patch('urllib.request.urlopen',return_value=response):
            self.assertEqual(ai.parse('收入10元已分红到个人账户',conf)['status'],'distributed')
            result['income_distributed']=False
            response.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':json.dumps(result)}}]}).encode()
            self.assertEqual(ai.parse('收入10元，计划以后分红',conf)['status'],'posted')
