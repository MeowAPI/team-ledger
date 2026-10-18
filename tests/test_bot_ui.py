import json
import time
import unittest
from unittest.mock import patch
from tests import test_ledger as base
from app import bot, bot_ui, config
from app.db import db, set_setting


class TelegramUiTest(unittest.TestCase):
    setUp = base.LedgerTest.setUp
    tearDown = base.LedgerTest.tearDown
    post = base.LedgerTest.post
    entry = base.LedgerTest.entry
    action = base.LedgerTest.action
    new_member = base.LedgerTest.new_member
    link_bot = base.LedgerTest.link_bot
    message = staticmethod(base.LedgerTest.message)
    callback = staticmethod(base.LedgerTest.callback)

    def last_message(self):
        with db() as conn:
            rows = conn.execute("SELECT * FROM tg_outbox WHERE method IN ('sendMessage','editMessageText') ORDER BY id DESC LIMIT 1").fetchall()
        return json.loads(rows[0]['payload'])

    def draft(self):
        with db() as conn:
            return dict(conn.execute('SELECT * FROM tg_drafts ORDER BY rowid DESC LIMIT 1').fetchone())

    def clear_outbox(self):
        with db(write=True) as conn:
            conn.execute('DELETE FROM tg_outbox')

    def prepare_payment(self, token, currency='CNY', amount='全额', start=100):
        self.click(start,f'd:{token}:paycur:{currency}')
        bot.process_update(self.message(start+1,amount))

    def click(self, update_id, data, chat=123, sender=123, private=True):
        update = self.callback(update_id, data, sender, chat)
        update['callback_query']['message'].update(message_id=77)
        if not private:
            update['callback_query']['message']['chat']['type'] = 'supergroup'
            update['callback_query']['message']['message_thread_id'] = 12
        bot.process_update(update)

    def seed_draft(self):
        payload={'kind':'expense','amount':'35','currency':'CNY','description':'打车','date':config.today(),'note':''}
        with patch('app.ai.parse', return_value=payload):
            bot.process_update(self.message(2, '今天打车35元'))
        return self.draft()['token']

    def allow_group(self):
        with db(write=True) as conn:
            set_setting(conn,'telegram_allowed_chats',['-555'])

    def flush_prompts(self):
        def deliver(method,payload):
            self.assertNotIn('_prompt',payload)
            self.assertNotIn('_thread_id',payload)
            if method == 'sendMessage':
                return {'message_id': 600}
            return True
        with patch('app.bot.api',side_effect=deliver):
            bot.drain_outbox()

    def test_home_lists_navigation_and_web_link(self):
        self.link_bot()
        with patch.dict('os.environ', {'PUBLIC_URL':'https://ledger.example'}):
            self.click(2,'home')
        payload=self.last_message()
        labels=[b['text'] for row in payload['reply_markup']['inline_keyboard'] for b in row]
        for fragment in ('最近账目','我的待报销','团队待报销','资金与付款项目','AI 记账'):
            self.assertTrue(any(fragment in label for label in labels))
        self.assertEqual(payload['message_id'],77)
        self.assertTrue(any(b.get('url')=='https://ledger.example' for row in payload['reply_markup']['inline_keyboard'] for b in row))

    def test_recent_entries_paginate_and_edit_original_message(self):
        self.link_bot()
        for i in range(7):
            self.entry(description=f'项目{i}')
        self.click(2,'list:recent:0')
        first=self.last_message()
        self.assertIn('第 1 / 2 页',first['text'])
        labels=[b for row in first['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(len([b for b in labels if b.get('callback_data','').startswith('e:')]),5)
        self.assertTrue(any(b.get('callback_data')=='list:recent:1' for b in labels))
        self.click(3,'list:recent:1')
        self.assertIn('第 2 / 2 页',self.last_message()['text'])
        self.assertNotIn('项目6',self.last_message()['text'])

    def test_personal_list_filters_other_members_and_shows_reference_currency(self):
        other=self.new_member(); self.link_bot()
        self.entry(description='自己的账',currency='USD',amount='2')
        self.entry(description='其他成员的账',payer_id=other['id'])
        bot.process_update(self.message(2,'/summary'))
        text=self.last_message()['text']
        self.assertIn('自己的账',text)
        self.assertNotIn('其他成员的账',text)
        self.assertIn('≈ ¥14.00 CNY',text)
        self.assertIn('2026-10-16',text)

    def test_ai_button_then_text_produces_editable_draft(self):
        self.link_bot(); self.click(2,'ai')
        self.assertEqual(self.draft()['step'],'ai_text')
        self.assertTrue(self.last_message()['reply_markup']['force_reply'])
        with patch('app.ai.parse',return_value={'kind':'expense','amount':'9','currency':'CNY','description':'午饭','date':config.today(),'note':''}):
            bot.process_update(self.message(3,'午饭9元'))
        self.assertEqual(self.draft()['step'],'confirm')
        callbacks=[b['callback_data'] for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row]
        self.assertTrue(any(c.endswith(':edit:amount') for c in callbacks))
        self.assertTrue(any(c.endswith(':edit:note') for c in callbacks))
        self.assertEqual(self.client.get('/api/entries').json(),[])

    def test_preview_edits_amount_note_date_and_payer_before_save(self):
        member=self.new_member(); self.link_bot(); token=self.seed_draft()
        self.click(3,f'd:{token}:edit:amount')
        bot.process_update(self.message(4,'45.50'))
        self.click(5,f'd:{token}:edit:note')
        bot.process_update(self.message(6,'客户拜访'))
        self.click(7,f'd:{token}:edit:date')
        self.click(8,f'd:{token}:day:1')
        self.click(9,f'd:{token}:edit:payer')
        self.click(10,f'd:{token}:payer:{member["id"]}')
        self.click(11,f'd:{token}:save')
        entry=self.client.get('/api/entries').json()[0]
        self.assertEqual(entry['amount'],'45.50')
        self.assertEqual(entry['note'],'客户拜访')
        self.assertEqual(entry['payer_id'],member['id'])
        self.assertNotEqual(entry['date'],config.today())

    def test_currency_edit_requires_amount_in_new_currency_precision(self):
        self.link_bot(); token=self.seed_draft()
        self.click(3,f'd:{token}:edit:currency')
        self.click(4,f'd:{token}:cur:JPY')
        self.assertEqual(self.draft()['step'],'edit_amount')
        bot.process_update(self.message(5,'3.5'))
        self.assertEqual(self.draft()['step'],'edit_amount')
        bot.process_update(self.message(6,'500'))
        self.assertEqual(self.draft()['step'],'confirm')
        self.click(7,f'd:{token}:save')
        self.assertEqual(self.client.get('/api/entries').json()[0]['amount'],'500')

    def test_payment_needs_note_and_confirmation_and_replay_is_safe(self):
        self.link_bot(); entry=self.entry()
        self.click(2,f'pay:{entry["id"]}')
        token=self.draft()['token']
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'pending')
        self.click(3,f'd:{token}:commit')
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'pending')
        self.prepare_payment(token)
        bot.process_update(self.message(4,'银行转账 TX42'))
        self.assertIn('不会发起转账',self.last_message()['text'])
        self.click(5,f'd:{token}:commit')
        self.click(6,f'd:{token}:commit')
        result=self.client.get('/api/entries').json()[0]
        self.assertEqual(result['status'],'reimbursed')
        self.assertEqual(result['review_note'],'银行转账 TX42')
        events=self.client.get(f'/api/entries/{entry["id"]}/events').json()
        self.assertEqual(len([e for e in events if e['action']=='pay']),1)

    def test_void_cancel_and_confirmation_keep_history(self):
        self.link_bot(); entry=self.entry(kind='income')
        self.click(2,f'void:{entry["id"]}'); token=self.draft()['token']
        bot.process_update(self.message(3,'重复录入'))
        self.click(4,f'd:{token}:cancel')
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'posted')
        self.click(5,f'void:{entry["id"]}'); token=self.draft()['token']
        bot.process_update(self.message(6,'重复录入'))
        self.click(7,f'd:{token}:commit')
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'voided')

    def test_partial_payment_can_use_settlement_then_original_currency(self):
        self.link_bot(); entry=self.entry(amount='100',currency='USD')
        self.click(2,f'pay:{entry["id"]}'); token=self.draft()['token']
        labels=[b['text'] for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row]
        self.assertTrue(any('按记账货币 · USD' in label for label in labels))
        self.assertTrue(any('按结算货币 · CNY' in label for label in labels))
        self.prepare_payment(token,'CNY','280')
        bot.process_update(self.message(3,'银行第一笔'))
        self.assertIn('¥280.00 CNY',self.last_message()['text'])
        self.assertIn('剩余待报：$60.00 USD',self.last_message()['text'])
        self.click(4,f'd:{token}:commit')
        self.assertIn('部分报销',self.last_message()['text'])
        self.assertIn('累计已报：$40.00 USD',self.last_message()['text'])
        self.click(5,'list:mine:0')
        self.assertIn('合计：$60.00 USD',self.last_message()['text'])
        self.click(6,f'pay:{entry["id"]}'); token=self.draft()['token']
        self.prepare_payment(token,'USD','全额',start=110)
        bot.process_update(self.message(7,'银行第二笔'))
        self.click(8,f'd:{token}:commit')
        result=self.client.get('/api/entries').json()[0]
        self.assertEqual(result['status'],'reimbursed')
        self.assertEqual([p['currency'] for p in result['payments']],['CNY','USD'])
        self.assertEqual([p['amount'] for p in result['payments']],['280.00','60.00'])

    def test_partial_payment_rejects_overpayment_and_stale_web_installment(self):
        self.link_bot(); entry=self.entry(amount='100')
        self.click(2,f'pay:{entry["id"]}'); token=self.draft()['token']
        self.click(3,f'd:{token}:paycur:CNY')
        bot.process_update(self.message(4,'100.01'))
        self.assertEqual(self.draft()['step'],'pay_amount')
        bot.process_update(self.message(5,'40'))
        bot.process_update(self.message(6,'机器人付款'))
        self.assertEqual(self.action(entry,'pay','Web 付款',amount='20').status_code,200)
        self.click(7,f'd:{token}:commit')
        result=self.client.get('/api/entries').json()[0]
        self.assertEqual(result['reimbursed_amount'],'20.00')
        self.assertEqual(len(result['payments']),1)

    def test_payment_currency_and_amount_can_be_changed_before_confirmation(self):
        self.link_bot(); entry=self.entry(amount='100',currency='USD')
        self.click(2,f'pay:{entry["id"]}'); token=self.draft()['token']
        self.prepare_payment(token,'CNY','280')
        bot.process_update(self.message(3,'转账'))
        self.click(4,f'd:{token}:paychange')
        self.assertEqual(self.draft()['step'],'pay_currency')
        self.prepare_payment(token,'USD','10',start=110)
        bot.process_update(self.message(5,'实际支付美元'))
        self.click(6,f'd:{token}:commit')
        result=self.client.get('/api/entries').json()[0]
        self.assertEqual((result['reimbursed_amount'],result['payments'][0]['currency']),('10.00','USD'))

    def test_settlement_quote_only_fetches_the_payment_currency_pair(self):
        self.link_bot(); entry=self.entry(amount='100',currency='USD')
        self.click(2,f'pay:{entry["id"]}'); token=self.draft()['token']
        with patch('app.fx.rates_for',return_value={'base':'CNY','rates':{'USD':{'rate':'7','date':'2026-10-16'}},'missing':[],'stale':False}) as rates:
            self.click(3,f'd:{token}:paycur:CNY')
        self.assertEqual(rates.call_args.args,('CNY',{'USD'}))
        self.assertEqual(self.draft()['step'],'pay_amount')

    def test_existing_entry_edit_detects_same_second_web_change(self):
        self.link_bot(); entry=self.entry()
        self.click(2,f'edit:{entry["id"]}'); token=self.draft()['token']
        self.click(3,f'd:{token}:edit:description')
        bot.process_update(self.message(4,'机器人修改'))
        response=self.client.put('/api/entries/'+str(entry['id']),json={**entry,'amount':'99','description':'Web 修改'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.click(5,f'd:{token}:save')
        result=self.client.get('/api/entries').json()[0]
        self.assertEqual(result['amount'],'99.00')
        self.assertEqual(result['description'],'Web 修改')

    def test_existing_entry_edit_saves_without_creating_duplicate(self):
        self.link_bot(); entry=self.entry()
        self.click(2,f'edit:{entry["id"]}'); token=self.draft()['token']
        self.click(3,f'd:{token}:edit:description')
        bot.process_update(self.message(4,'补充完整用途'))
        self.click(5,f'd:{token}:save')
        result=self.client.get('/api/entries').json()
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['description'],'补充完整用途')
        self.assertEqual(result[0]['status'],'pending')

    def test_payment_confirmation_rejects_changed_web_amount(self):
        self.link_bot(); entry=self.entry()
        self.click(2,f'pay:{entry["id"]}'); token=self.draft()['token']
        self.prepare_payment(token)
        bot.process_update(self.message(3,'已转账'))
        self.client.put('/api/entries/'+str(entry['id']),json={**entry,'amount':'120'},headers=self.headers)
        self.click(4,f'd:{token}:commit')
        result=self.client.get('/api/entries').json()[0]
        self.assertEqual(result['status'],'pending')
        self.assertEqual(result['amount'],'120.00')

    def test_different_member_cannot_confirm_payment_draft(self):
        other=self.new_member(); self.link_bot(); entry=self.entry()
        with db(write=True) as conn:
            conn.execute('INSERT INTO tg_links VALUES (?,?)',('456',other['id']))
        self.click(2,f'pay:{entry["id"]}'); token=self.draft()['token']
        self.prepare_payment(token)
        bot.process_update(self.message(3,'现金'))
        self.click(4,f'd:{token}:commit',sender=456)
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'pending')

    def test_group_conversation_does_not_trigger_ai_or_fill_draft(self):
        self.link_bot(); self.allow_group()
        with patch('app.ai.parse') as parse:
            bot.process_update(self.message(2,'今天开会',chat=-555,private=False))
            parse.assert_not_called()
        self.click(3,'new',chat=-555,private=False)
        token=self.draft()['token']
        self.click(4,f'd:{token}:cur:CNY',chat=-555,private=False)
        self.flush_prompts()
        bot.process_update(self.message(5,'88',chat=-555,private=False))
        self.assertEqual(self.draft()['step'],'amount')
        update=self.message(6,'88',chat=-555,private=False)
        update['message']['reply_to_message']={'message_id':600}
        update['message']['message_thread_id']=12
        bot.process_update(update)
        self.assertEqual(self.draft()['step'],'description')
        self.assertEqual(self.last_message()['message_thread_id'],12)
        bot.process_update({**update,'update_id':7})
        self.assertEqual(self.draft()['step'],'description')
        self.assertNotIn('description',json.loads(self.draft()['payload']))

    def test_prompts_for_other_draft_are_not_consumed_in_group(self):
        self.link_bot(); self.allow_group(); other=self.new_member()
        with db(write=True) as conn:
            conn.execute('INSERT INTO tg_links VALUES (?,?)',('456',other['id']))
        self.click(2,'new',chat=-555,private=False)
        first=self.draft()['token']
        self.click(3,f'd:{first}:cur:CNY',chat=-555,private=False)
        self.flush_prompts()
        self.click(4,'new',chat=-555,sender=456,private=False)
        second=self.draft()['token']
        self.click(5,f'd:{second}:cur:CNY',chat=-555,sender=456,private=False)
        update=self.message(6,'60',sender=456,chat=-555,private=False)
        update['message']['reply_to_message']={'message_id':600}
        bot.process_update(update)
        self.assertEqual(self.draft()['step'],'amount')

    def test_other_bot_command_is_ignored(self):
        self.link_bot(); self.clear_outbox()
        with patch('app.ai.parse') as parse:
            bot.process_update(self.message(2,'/ai@other_bot 打车35元'))
            parse.assert_not_called()
        with db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_outbox').fetchone()[0],0)

    def test_funds_lists_projects_and_partial_budget(self):
        self.link_bot()
        self.entry(kind='income',amount='15')
        self.entry(amount='10',description='项目甲')
        self.entry(amount='10',description='项目乙')
        bot.process_update(self.message(2,'/funds'))
        text=self.last_message()['text']
        self.assertIn('项目甲',text); self.assertIn('项目乙',text)
        self.assertIn('剩余全额可安排',text); self.assertIn('可部分报销',text)
        self.assertIn('建议 ¥5.00 CNY',text)

    def test_today_excludes_voided_income_and_drafts(self):
        self.link_bot()
        self.entry(kind='income',amount='100')
        self.entry(amount='15')
        self.entry(amount='600',status='draft')
        bad=self.entry(kind='income',amount='999'); self.action(bad,'void','重复')
        bot.process_update(self.message(2,'/today'))
        text=self.last_message()['text']
        self.assertIn('¥100.00',text); self.assertIn('¥15.00',text)
        self.assertNotIn('600',text); self.assertNotIn('999',text)

    def test_outbox_ignores_expired_callback_and_unchanged_edit(self):
        self.link_bot(); self.clear_outbox()
        with db(write=True) as conn:
            bot.queue(conn,'answerCallbackQuery',{'callback_query_id':'expired'})
            bot.queue(conn,'editMessageText',{'chat_id':123,'message_id':77,'text':'same'})
            bot.queue(conn,'sendMessage',{'chat_id':123,'text':'next'})
        def send(method,payload):
            if method=='answerCallbackQuery':raise bot.TelegramError(400,'query is too old')
            if method=='editMessageText':raise bot.TelegramError(400,'message is not modified')
            return {'message_id':900}
        with patch('app.bot.api',side_effect=send):bot.drain_outbox()
        with db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_outbox WHERE sent=0').fetchone()[0],0)

    def test_invalid_callback_does_not_poison_update_stream(self):
        self.link_bot()
        self.click(2,'list:mine:NaN')
        self.click(3,'e:missing')
        self.click(4,'home')
        self.assertIn('团队账本',self.last_message()['text'])
        with db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_updates').fetchone()[0],4)

    def test_command_menu_registered_without_sending_messages(self):
        with patch('app.bot.api',return_value=True) as api:
            bot.configure_commands()
        self.assertEqual(api.call_args_list[0].args[0],'setMyCommands')
        commands=api.call_args_list[0].args[1]['commands']
        self.assertTrue({'start','recent','funds','today','month','team'} <= {c['command'] for c in commands})
        self.assertEqual(api.call_args_list[1].args[0],'setChatMenuButton')

    def test_home_can_resume_current_draft(self):
        self.link_bot(); token=self.seed_draft()
        self.click(3,'home')
        buttons=[b for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row]
        self.assertTrue(any(b['callback_data']==f'd:{token}:resume' for b in buttons if 'callback_data' in b))
        self.click(4,f'd:{token}:resume')
        self.assertIn('打车',self.last_message()['text'])

    def test_existing_web_draft_can_be_posted_after_confirmation(self):
        self.link_bot(); entry=self.entry(status='draft')
        self.click(2,f'post:{entry["id"]}'); token=self.draft()['token']
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'draft')
        self.click(3,f'd:{token}:commit')
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'],'pending')


if __name__=='__main__':
    unittest.main()
