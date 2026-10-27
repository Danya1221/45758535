"""Full offline Telegram path: group query, private quote, buyer follow-up."""
import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot
from telethon import events, types

from catalog_bot.app import Application, Config
from catalog_bot.catalog import Product
from catalog_bot.storage import Store
from tests import test_app as bot_helpers
from tests import test_monitor_flow as monitor_helpers


class PrivateConversationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(123, 'test', '', 55, Path(self.temp.name))
        self.path = Path(self.temp.name) / 'catalog.sqlite3'
        self.store = Store(self.path)
        self.store.set('telegram_account_id', 77)
        self.store.set('active', True)
        self.products = [Product('Galaxy S25 12/256 Navy', Decimal('49000'), None)]
        self.store.replace_products(self.products)
        self.store.toggle_chat(-123, 'Закупки')
        self.transport = bot_helpers.LocalSession()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.transport)
        self.client = monitor_helpers.DispatchClient()
        self.next_message_id = 900

        async def sent(*args, **kwargs):
            result = SimpleNamespace(id=self.next_message_id)
            self.next_message_id += 1
            return result

        self.client.send_message = AsyncMock(side_effect=sent)
        self.bind_app()

    def bind_app(self):
        self.app = Application(self.config, self.store, self.client, self.bot)
        self.app.auth.ready = True
        self.app.account_available.set()
        self.client.add_event_handler(self.app.monitor, events.NewMessage())
        self.client.add_event_handler(self.app.private_reply, events.NewMessage(incoming=True))

    async def asyncTearDown(self):
        task = self.app.group_directory.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await self.bot.session.close()
        self.store.close()
        self.temp.cleanup()

    def update(self, text, *, group=False, number=200, sender=88, username='buyer_test', reply_to=None,
               quote_text=None, reply_peer=None):
        message = types.Message(id=number,
                                peer_id=types.PeerChat(123) if group else types.PeerUser(sender),
                                from_id=types.PeerUser(sender),
                                date=datetime.fromtimestamp(int(time.time()), timezone.utc),
                                message=text, out=False,
                                reply_to=types.MessageReplyHeader(reply_to_msg_id=reply_to, quote_text=quote_text,
                                                                   quote=quote_text is not None,
                                                                   reply_to_peer_id=reply_peer) if reply_to else None)
        update = types.UpdateNewMessage(message=message, pts=20, pts_count=1)
        update._entities = {sender: types.User(id=sender, first_name='Илья', access_hash=800, username=username)}
        return update

    def notices(self):
        return [call for call in self.transport.calls if call.__api_method__ == 'sendMessage']

    async def test_group_quote_then_discount_and_hold_use_private_user_and_tagged_owner_notice(self):
        await self.client._dispatch_update(self.update('s25 navy', group=True, number=28))
        quote = self.store.latest_quote(77, 88)
        self.assertIsNotNone(quote)
        self.assertEqual(quote['products'], self.products)
        self.assertEqual(quote['sent_message_ids'], [900])
        self.assertIn('@buyer_test', self.notices()[0].text)
        self.assertNotIn('Получатель: 88', self.notices()[0].text)
        await self.client._dispatch_update(self.update('-200', number=201, reply_to=900))
        self.assertEqual(self.client.send_message.await_args.args[1], 'Нет')
        await self.client._dispatch_update(self.update('отложите', number=202, reply_to=900))
        call = self.client.send_message.await_args
        self.assertEqual(call.args, (types.InputPeerUser(88, 800), 'Ок'))
        self.assertNotIn('reply_to', call.kwargs)
        self.assertIn('@buyer_test', self.notices()[-1].text)
        self.assertIn('Galaxy S25 12/256 Navy', self.notices()[-1].text)
        self.assertIn('49 000 ₽', self.notices()[-1].text)
        self.assertEqual(self.notices()[-1].parse_mode, 'HTML')
        self.assertEqual(self.store.products(), self.products)
        self.assertEqual(self.store.holds(77, 88, quote['quote_id'])[0]['status'], 'sent')

    async def test_ambiguous_hold_then_reply_to_clarification_selects_the_chosen_offer(self):
        black = Product('Galaxy S25 12/512 Black', Decimal('50000'), None)
        self.store.replace_products(self.products + [black])
        await self.client._dispatch_update(self.update('s25', group=True, number=28))
        await self.client._dispatch_update(self.update('отложите', number=201, reply_to=900))
        self.assertIn('Какую именно позицию', self.client.send_message.await_args.args[1])
        self.assertEqual(len(self.notices()), 1)
        await self.client._dispatch_update(self.update('черный 512', number=202, reply_to=901))
        self.assertEqual(self.client.send_message.await_args.args[1], 'Ок')
        self.assertIn('12/512 Black', self.notices()[-1].text)
        self.assertNotIn('12/256 Navy', self.notices()[-1].text)

    async def test_no_quote_untracked_reply_other_buyer_and_forward_are_not_answered(self):
        await self.client._dispatch_update(self.update('отложите'))
        self.client.send_message.assert_not_awaited()
        await self.client._dispatch_update(self.update('s25 navy', group=True, number=28))
        self.client.send_message.reset_mock()
        await self.client._dispatch_update(self.update('отложите', number=201, sender=89))
        await self.client._dispatch_update(self.update('отложите', number=202, reply_to=999))
        forwarded = self.update('отложите', number=203)
        forwarded.message.fwd_from = types.MessageFwdHeader(date=datetime.now(timezone.utc))
        await self.client._dispatch_update(forwarded)
        self.client.send_message.assert_not_awaited()

    async def test_restart_preserves_quote_and_duplicate_hold_does_not_notify_again(self):
        await self.client._dispatch_update(self.update('s25 navy', group=True, number=28, username=None))
        self.client.remove_event_handler(self.app.monitor)
        self.client.remove_event_handler(self.app.private_reply)
        self.store.close()
        self.store = Store(self.path)
        self.bind_app()
        hold = self.update('отложите', number=201, reply_to=900, username=None)
        await self.client._dispatch_update(hold)
        self.assertEqual(self.client.send_message.await_args.args[1], 'Ок')
        self.assertIn('<a href="tg://user?id=88">Илья</a>', self.notices()[-1].text)
        count, notices = self.client.send_message.await_count, len(self.notices())
        await self.client._dispatch_update(hold)
        self.assertEqual(self.client.send_message.await_count, count)
        self.assertEqual(len(self.notices()), notices)

    async def test_screenshot_selected_text_quote_is_passed_from_telegram_to_hold_selection(self):
        black = Product('Galaxy S26 12/256 Black SM-S942B/DS', Decimal('57300'), None, country='🇿🇦')
        blue = Product('Galaxy S26 12/256 Sky Blue SM-S942B/DS', Decimal('58000'), None, country='🇿🇦')
        self.store.replace_products([black, blue])
        await self.client._dispatch_update(self.update('s26', group=True, number=28))
        text = '• Galaxy S26 12/256 Black SM-S942B/DS\n🇿🇦 — 57 300 ₽'
        await self.client._dispatch_update(self.update('отложи', number=201, reply_to=900, quote_text=text))
        self.assertEqual(self.client.send_message.await_args.args[1], 'Ок')
        self.assertIn('Black SM-S942B/DS', self.notices()[-1].text)
        self.assertIn('57 300 ₽', self.notices()[-1].text)
        self.assertNotIn('Sky Blue', self.notices()[-1].text)

    async def test_reply_with_a_partial_unique_quote_selects_that_offer(self):
        black = Product('Galaxy S25 12/512 Black', Decimal('50000'), None)
        self.store.replace_products(self.products + [black])
        await self.client._dispatch_update(self.update('s25', group=True, number=28))
        await self.client._dispatch_update(self.update('отложите', number=201, reply_to=900, quote_text='12/512 Black'))
        self.assertEqual(self.client.send_message.await_args.args[1], 'Ок')
        self.assertIn('12/512 Black', self.notices()[-1].text)

    async def test_quote_referencing_a_different_dialog_is_not_used(self):
        await self.client._dispatch_update(self.update('s25 navy', group=True, number=28))
        self.client.send_message.reset_mock()
        await self.client._dispatch_update(self.update('отложи', number=201, reply_to=900,
                                                      quote_text='Galaxy S25', reply_peer=types.PeerUser(99)))
        self.client.send_message.assert_not_awaited()
