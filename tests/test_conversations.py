import asyncio
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from catalog_bot.catalog import Product, build_reply
from catalog_bot.conversations import ConversationReplies, user_tag
from catalog_bot.engine import Engine, Incoming, SendFailure
from catalog_bot.storage import Store


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.store = Store(self.path)
        self.store.set('telegram_account_id', 77)
        self.store.set('active', True)
        self.products = [Product('Galaxy S25 12/256 Navy', Decimal('49000'), None)]
        self.store.replace_products(self.products)
        self.quote_id = self.store.save_quote(77, 88, -123, 28, 's25 navy', self.products,
                                              now=1000, sent_message_ids=[900])
        self.sender = SimpleNamespace(id=88, username='buyer_test', first_name='Buyer', last_name=None)
        self.send, self.notify = AsyncMock(return_value=[901]), AsyncMock(return_value=True)
        self.now = 1001
        self.flow = ConversationReplies(self.store, self.send, self.notify, asyncio.Lock(), clock=lambda: self.now)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, text='отложите', **changes):
        data = dict(chat_id=88, message_id=200, sender_id=88, text=text, timestamp=self.now, is_group=False)
        return Incoming(**(data | changes))

    async def test_discount_answers_no_without_hold_or_owner_notice(self):
        self.assertEqual(await self.flow.process(self.message('-200'), self.sender), 'discount')
        self.send.assert_awaited_once_with(88, 200, 'Нет')
        self.notify.assert_not_awaited()
        self.assertEqual(self.store.holds(77, 88, self.quote_id), [])
        self.assertEqual(self.store.followup(77, 88, 200)['status'], 'sent')

    async def test_hold_silently_notifies_owner_with_tag_and_exact_quoted_price(self):
        self.store.replace_products([replace(self.products[0], price=Decimal('99999'))])
        self.assertEqual(await self.flow.process(self.message(), self.sender), 'hold')
        self.send.assert_not_awaited()
        notice = self.notify.await_args
        self.assertIn('@buyer_test', notice.args[0])
        self.assertIn('Galaxy S25 12/256 Navy', notice.args[0])
        self.assertIn('49 000 ₽', notice.args[0])
        self.assertNotIn('99 999', notice.args[0])
        self.assertEqual(notice.kwargs['parse_mode'], 'HTML')
        self.assertEqual(self.store.holds(77, 88, self.quote_id)[0]['status'], 'sent')
        self.assertEqual(self.store.products()[0].price, Decimal('99999'))

    async def test_duplicate_event_and_repeated_hold_never_duplicate_owner_notice(self):
        self.assertEqual(await self.flow.process(self.message(), self.sender), 'hold')
        self.assertEqual(await self.flow.process(self.message(), self.sender), 'duplicate')
        self.assertEqual(await self.flow.process(self.message(message_id=201), self.sender), 'hold')
        self.send.assert_not_awaited()
        self.notify.assert_awaited_once()
        self.assertEqual(len(self.store.holds(77, 88, self.quote_id)), 1)

    async def test_multiple_offers_notify_owner_then_buyer_selection_is_silent(self):
        black = Product('Galaxy S25 12/512 Black', Decimal('50000'), None)
        self.store.save_quote(77, 88, -123, 29, 's25', self.products + [black], now=1001, sent_message_ids=[910])
        self.assertEqual(await self.flow.process(self.message(), self.sender), 'clarify')
        self.send.assert_not_awaited()
        self.assertIn('отложите', self.notify.await_args.args[0].lower())
        self.assertIn('уточнить', self.notify.await_args.args[0].lower())
        self.assertEqual(await self.flow.process(self.message('черный 512', message_id=201), self.sender, 910), 'hold')
        self.send.assert_not_awaited()
        self.assertIn('12/512 Black', self.notify.await_args.args[0])
        self.assertNotIn('12/256 Navy', self.notify.await_args.args[0])

    async def test_reply_to_old_quote_uses_that_quote_and_untracked_reply_has_no_fallback(self):
        other = Product('iPhone 17 256 Black', Decimal('85000'), None)
        self.store.save_quote(77, 88, -123, 29, 'iPhone 17', [other], now=1001, sent_message_ids=[910])
        self.assertEqual(await self.flow.process(self.message(), self.sender, 999), 'untracked')
        self.send.assert_not_awaited()
        self.assertEqual(await self.flow.process(self.message(), self.sender, 900), 'hold')
        self.assertIn('Galaxy S25', self.notify.await_args.args[0])
        self.assertNotIn('iPhone', self.notify.await_args.args[0])
        self.send.assert_not_awaited()

    async def test_copied_offer_with_price_and_region_can_select_a_specific_variant(self):
        black = Product('Galaxy S25 12/512 Black', Decimal('50000'), None, country='🇨🇱')
        self.store.save_quote(77, 88, -123, 29, 's25', self.products + [black], now=1001, sent_message_ids=[910])
        self.assertEqual(await self.flow.process(self.message(), self.sender), 'clarify')
        row = build_reply([black], intro='')
        self.assertEqual(await self.flow.process(self.message(row, message_id=201), self.sender), 'hold')
        self.assertIn('12/512 Black', self.notify.await_args.args[0])
        self.assertIn('50 000 ₽', self.notify.await_args.args[0])

    async def test_selected_quote_row_books_the_exact_model_region_and_price(self):
        black = Product('Galaxy S26 12/256 Black SM-S942B/DS', Decimal('57300'), None, country='🇿🇦')
        other = replace(black, country='🇭🇰', price=Decimal('58000'))
        self.store.save_quote(77, 88, -123, 29, 's26', [black, other], now=1001, sent_message_ids=[910])
        quoted = '• Galaxy S26 12/256 Black SM-S942B/DS\n🇿🇦 — 57 300 ₽'
        result = await self.flow.process(self.message('отложи'), self.sender, 910, quoted_text=quoted)
        self.assertEqual(result, 'hold')
        self.send.assert_not_awaited()
        notice = self.notify.await_args.args[0]
        self.assertIn('🇿🇦', notice)
        self.assertIn('57 300 ₽', notice)
        self.assertNotIn('58 000', notice)

    async def test_selected_quote_without_bullet_books_the_exact_offer(self):
        black = Product('Galaxy S26 12/256 Black', Decimal('57300'), None, country='🇿🇦')
        self.store.save_quote(77, 88, -123, 29, 's26', [black], now=1001, sent_message_ids=[910])
        quoted = build_reply([black], intro='')
        result = await self.flow.process(self.message('отложи'), self.sender, 910, quoted_text=quoted)
        self.assertEqual(result, 'hold')
        self.send.assert_not_awaited()
        self.assertIn('57 300 ₽', self.notify.await_args.args[0])

    async def test_ambiguous_or_unquoted_text_never_selects_a_random_offer(self):
        black = Product('Galaxy S25 12/256 Black', Decimal('49000'), None)
        self.store.save_quote(77, 88, -123, 29, 's25', self.products + [black], now=1001, sent_message_ids=[910])
        for number, quoted in enumerate(('Galaxy S25', 'iPhone 17', '• Galaxy S25 12/256 Black — 1 ₽'), 201):
            result = await self.flow.process(self.message(message_id=number), self.sender, 910, quoted_text=quoted)
            self.assertEqual(result, 'clarify')
            self.send.assert_not_awaited()
            self.assertIn('уточнить', self.notify.await_args.args[0].lower())
        self.assertFalse(self.store.holds(77, 88, self.store.latest_quote(77, 88, now=self.now)['quote_id']))

    async def test_explicit_selection_in_message_must_agree_with_selected_quote(self):
        black = Product('Galaxy S25 12/256 Black', Decimal('49000'), None)
        self.store.save_quote(77, 88, -123, 29, 's25', self.products + [black], now=1001, sent_message_ids=[910])
        result = await self.flow.process(self.message('отложите navy'), self.sender, 910, quoted_text='Black')
        self.assertEqual(result, 'clarify')
        self.send.assert_not_awaited()
        self.assertIn('уточнить', self.notify.await_args.args[0].lower())

    async def test_text_quote_requires_a_tracked_reply_message(self):
        row = build_reply(self.products, intro='')
        self.assertEqual(await self.flow.process(self.message(), self.sender, quoted_text=row), 'untracked')
        self.assertEqual(await self.flow.process(self.message(), self.sender, 999, quoted_text=row), 'untracked')
        self.send.assert_not_awaited()

    async def test_discount_in_reply_body_keeps_priority_over_quoted_product(self):
        result = await self.flow.process(self.message('-200 и отложите'), self.sender, 900,
                                         quoted_text=build_reply(self.products, intro=''))
        self.assertEqual(result, 'discount')
        self.send.assert_awaited_once_with(88, 200, 'Нет')
        self.notify.assert_not_awaited()

    async def test_other_buyer_account_and_expired_quote_cannot_trigger_replies(self):
        other = SimpleNamespace(id=89, username='someone_else')
        self.assertEqual(await self.flow.process(self.message(sender_id=89, chat_id=89), other), 'untracked')
        self.store.set('telegram_account_id', 78)
        self.assertEqual(await self.flow.process(self.message(), self.sender), 'untracked')
        self.store.set('telegram_account_id', 77)
        self.now = 1000 + 86401
        self.assertEqual(await self.flow.process(self.message(), self.sender), 'untracked')
        self.send.assert_not_awaited()

    async def test_unrelated_unsafe_stale_and_paused_messages_are_not_answered(self):
        for changes in ({'is_group': True}, {'outgoing': True}, {'is_bot': True},
                        {'forwarded': True}, {'sender_id': -1}, {'chat_id': 99},
                        {'text': 'Спасибо'}, {'text': 'Не откладывайте'}, {'timestamp': 500}):
            with self.subTest(changes=changes):
                self.assertNotIn(await self.flow.process(self.message(**changes), self.sender), {'hold', 'discount', 'clarify'})
        self.store.set('active', False)
        self.assertEqual(await self.flow.process(self.message(), self.sender), 'paused')
        self.send.assert_not_awaited()
        self.notify.assert_not_awaited()

    async def test_recipient_failure_does_not_pause_others_or_notify_false_hold(self):
        self.send.side_effect = SendFailure('UserPrivacyRestrictedError', pause=False)
        self.assertEqual(await self.flow.process(self.message('-200'), self.sender), 'failed')
        self.assertTrue(self.store.get('active'))
        self.notify.assert_not_awaited()
        self.assertEqual(self.store.holds(77, 88, self.quote_id), [])

    async def test_uncertain_send_pauses_and_is_not_replayed_after_restart(self):
        self.send.side_effect = SendFailure('TimeoutError', uncertain=True)
        self.assertEqual(await self.flow.process(self.message('-200'), self.sender), 'uncertain')
        self.assertFalse(self.store.get('active'))
        self.notify.assert_not_awaited()
        self.store.close()
        self.store = Store(self.path)
        self.store.set('active', True)
        self.flow.store = self.store
        self.assertEqual(await self.flow.process(self.message('-200'), self.sender), 'duplicate')
        self.send.assert_awaited_once()

    async def test_owner_notice_failure_is_recorded_and_pauses_new_replies(self):
        self.notify.return_value = False
        self.assertEqual(await self.flow.process(self.message(), self.sender), 'notice_failed')
        self.assertFalse(self.store.get('active'))
        self.send.assert_not_awaited()
        held = self.store.holds(77, 88, self.quote_id)[0]
        self.assertEqual((held['status'], held['detail']), ('uncertain', 'owner_notification_failed'))
        followup = self.store.followup(77, 88, 200)
        self.assertEqual((followup['status'], followup['detail']), ('uncertain', 'owner_notification_failed'))

    async def test_hold_notification_is_not_limited_by_customer_reply_limit(self):
        self.flow.max_per_minute = 1
        self.assertEqual(await self.flow.process(self.message('-200'), self.sender), 'discount')
        self.assertEqual(await self.flow.process(self.message(message_id=201), self.sender), 'hold')
        self.send.assert_awaited_once()
        self.notify.assert_awaited_once()

    async def test_followup_written_during_multipart_quote_is_not_treated_as_pre_quote(self):
        self.store.toggle_chat(-123, 'Закупки')

        async def slow_quote(*args):
            self.now = 1006
            return [920, 921]

        engine = Engine(self.store, slow_quote, clock=lambda: self.now)
        self.assertEqual(await engine.process(Incoming(-123, 29, 88, 's25 navy', 1001)), 'sent')
        quote = self.store.quote_for_reply(77, 88, 920, now=self.now)
        self.assertEqual(quote['created'], 1001)
        self.assertEqual(await self.flow.process(self.message(timestamp=1002), self.sender, 920), 'hold')
        self.send.assert_not_awaited()

    async def test_fresh_followup_does_not_expire_while_waiting_for_quote_parts(self):
        await self.flow.lock.acquire()
        task = asyncio.create_task(self.flow.process(self.message(), self.sender))
        try:
            await asyncio.sleep(0)
            self.now += 200
        finally:
            self.flow.lock.release()
        self.assertEqual(await task, 'hold')
        self.send.assert_not_awaited()

    def test_tags_use_username_or_clickable_escaped_name_without_numeric_label(self):
        self.assertEqual(user_tag(self.sender), '@buyer_test')
        tag = user_tag(SimpleNamespace(id=88, username=None, first_name='<Илья &>', last_name='Тест'))
        self.assertEqual(tag, '<a href="tg://user?id=88">&lt;Илья &amp;&gt; Тест</a>')
        self.assertNotIn('>88<', tag)
