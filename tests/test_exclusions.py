import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, MessageOriginHiddenUser, MessageOriginUser, Update, User
from telethon import types

from catalog_bot.app import Application, Config
from catalog_bot.catalog import Product
from catalog_bot.conversations import ConversationReplies
from catalog_bot.engine import Engine, Incoming, SendFailure
from catalog_bot.monitor_diagnostics import diagnose_message, reason_label
from catalog_bot.storage import Store
from test_app import LocalClient, LocalSession


class ExclusionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'bot.sqlite3')
        self.store.replace_products([Product('Nintendo Switch 2')])
        self.store.toggle_chat(-100, 'test')
        self.store.set('active', True)
        self.store.set('telegram_account_id', 77)
        self.send = AsyncMock(return_value=[900])
        self.engine = Engine(self.store, self.send, clock=lambda: 1000)
        self.message = Incoming(-100, 1, 88, 'Nintendo Switch 2', 1000)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_exclusion_survives_restart_and_does_not_affect_others(self):
        self.store.exclude_user(88, 'buyer', 'Buyer')
        path = Path(self.tmp.name) / 'bot.sqlite3'
        self.store.close()
        self.store = Store(path)
        self.engine.store = self.store
        self.assertEqual(await self.engine.process(self.message), 'excluded')
        self.assertIsNone(self.store.delivery(-100, 1))
        self.assertEqual(diagnose_message(self.store, self.message, now=1000).result, 'excluded')
        self.assertIn('исключения', reason_label('excluded'))
        other = Incoming(-100, 2, 89, 'Nintendo Switch 2', 1000)
        self.assertEqual(await self.engine.process(other), 'sent')
        self.assertTrue(self.store.unexclude_user(88))
        self.assertEqual(await self.engine.process(self.message), 'sent')

    async def test_exclusion_during_delay_stops_pending_quote(self):
        self.engine.reply_delay_seconds = 10
        async def sleep(_):
            self.store.exclude_user(88, 'buyer', 'Buyer')
        with patch('catalog_bot.engine.asyncio.sleep', side_effect=sleep):
            self.assertEqual(await self.engine.process(self.message), 'excluded')
        self.send.assert_not_awaited()

    async def test_excluded_buyer_cannot_reserve_stock(self):
        product = Product('Nintendo Switch 2', stock=2)
        self.store.replace_products([product])
        self.store.save_quote(77, 88, -100, 1, 'Nintendo Switch 2', [product], now=999, sent_message_ids=[900])
        self.store.exclude_user(88, 'buyer', 'Buyer')
        flow = ConversationReplies(self.store, self.send, AsyncMock(), asyncio.Lock(), clock=lambda: 1000)
        sender = types.User(id=88, first_name='Buyer', username='buyer')
        result = await flow.process(Incoming(88, 2, 88, 'отложи', 1000, is_group=False), sender, 900)
        self.assertEqual(result, 'excluded')
        self.assertEqual(self.store.products()[0].stock, 2)


class ExclusionUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'bot.sqlite3')
        self.transport = LocalSession()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.transport)
        self.client = LocalClient()
        self.client.get_entity = AsyncMock(return_value=types.User(id=88, first_name='Buyer', username='buyername'))
        self.app = Application(Config(123, 'test', '', 55, Path(self.tmp.name)), self.store, self.client, self.bot)
        self.app.auth.ready = True
        self.app.account_available.set()
        self.counter = 0

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.store.close()
        self.tmp.cleanup()

    async def feed(self, text=None, *, origin=None, owner=55):
        self.counter += 1
        message = Message(message_id=self.counter, date=datetime.now(timezone.utc),
                          chat=Chat(id=owner, type='private'), from_user=User(id=owner, is_bot=False, first_name='Owner'),
                          text=text, forward_origin=origin)
        await self.app.dp.feed_update(self.bot, Update(update_id=self.counter, message=message))

    async def callback(self, data):
        self.counter += 1
        message = Message(message_id=100, date=datetime.now(timezone.utc), chat=Chat(id=55, type='private'))
        cb = CallbackQuery(id=str(self.counter), from_user=User(id=55, is_bot=False, first_name='Owner'),
                           chat_instance='1', message=message, data=data)
        await self.app.dp.feed_update(self.bot, Update(update_id=self.counter, callback_query=cb))

    async def test_add_by_username_and_remove_button(self):
        await self.feed('/exclude')
        await self.callback('exclude:add')
        await self.feed('@buyername')
        self.assertTrue(self.store.is_excluded(88))
        self.client.get_entity.assert_awaited_once_with('@buyername')
        self.assertEqual(self.store.excluded_users()[0]['username'], 'buyername')
        await self.callback('exclude:remove:88:0')
        self.assertFalse(self.store.is_excluded(88))

    async def test_forward_user_and_reject_hidden_and_bot(self):
        await self.callback('exclude:add')
        user = User(id=90, is_bot=False, first_name='Buyer', username='othername')
        await self.feed('hello', origin=MessageOriginUser(type='user', date=datetime.now(timezone.utc), sender_user=user))
        self.assertTrue(self.store.is_excluded(90))
        await self.callback('exclude:add')
        await self.feed('hello', origin=MessageOriginHiddenUser(type='hidden_user', date=datetime.now(timezone.utc), sender_user_name='Hidden'))
        self.assertEqual(len(self.store.excluded_users()), 1)
        self.client.get_entity.return_value = types.Channel(id=123, title='Channel', photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc))
        await self.feed('@channelname')
        self.assertEqual(len(self.store.excluded_users()), 1)

    async def test_excluding_during_recipient_lookup_prevents_send(self):
        async def lookup(_):
            self.store.exclude_user(88, 'buyer', 'Buyer')
            return types.InputPeerUser(88, 123)
        self.client.get_input_entity = AsyncMock(side_effect=lookup)
        self.client.send_message = AsyncMock()
        with self.assertRaisesRegex(SendFailure, 'excluded'):
            await self.app.send_reply(88, 1, 'price')
        self.client.send_message.assert_not_awaited()

    async def test_non_owner_cannot_manage_exclusions(self):
        await self.feed('/exclude @buyername', owner=66)
        self.assertFalse(self.store.is_excluded(88))
        self.client.get_entity.assert_not_awaited()
