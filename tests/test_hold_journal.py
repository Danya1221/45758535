import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from catalog_bot.app import Application, Config, menu
from catalog_bot.catalog import Product
from catalog_bot.conversations import ConversationReplies
from catalog_bot.engine import Incoming
from catalog_bot.storage import Store
from test_app import LocalClient, LocalSession


class HoldJournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'db.sqlite3'
        self.store = Store(self.path)
        self.store.set('telegram_account_id', 77)
        self.store.set('active', True)
        self.product = Product('Galaxy S26 12/256 Black SM-S942B/DS', Decimal('57000'), None, country='🇿🇦')
        self.store.replace_products([self.product])
        self.store.set('price_source', 'bot:999')
        self.quoted = replace(self.product, price=Decimal('57500'), source='supplier')
        self.quote_id = self.store.save_quote(77, 88, -123, 28, 's26 black', [self.quoted],
                                              now=1000, sent_message_ids=[900])
        self.sender = SimpleNamespace(id=88, username='buyer_test', first_name='Илья', last_name=None)
        self.send = AsyncMock()
        self.notify = AsyncMock(return_value=True)
        self.flow = ConversationReplies(self.store, self.send, self.notify, asyncio.Lock(), clock=lambda: 1001)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def message(self, text='отложи', message_id=200):
        return Incoming(88, message_id, 88, text, 1001, is_group=False)

    async def test_supplier_request_records_buyer_and_exact_quoted_price(self):
        self.assertEqual(await self.flow.process(self.message(), self.sender, 900), 'hold')
        rows = self.store.hold_requests()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['username'], rows[0]['buyer_name']), ('buyer_test', 'Илья'))
        self.assertIn('57 500 ₽', rows[0]['product_text'])
        self.assertNotIn('57 000', rows[0]['product_text'])
        self.assertNotIn('SM-S942B', rows[0]['product_text'])
        self.assertEqual((rows[0]['stock_status'], rows[0]['quantity']), ('supplier', 1))
        self.assertEqual(self.store.stock_reservations(), [])
        self.send.assert_not_awaited()

    async def test_history_survives_pruning_catalog_updates_and_restart(self):
        await self.flow.process(self.message(), self.sender, 900)
        self.store.replace_products([replace(self.product, price=Decimal('99999'))])
        self.store._prune_quote_context(1000 + 31 * 86400)
        self.store.close()
        self.store = Store(self.path)
        rows = self.store.hold_requests()
        self.assertEqual(len(rows), 1)
        self.assertIn('57 500 ₽', rows[0]['product_text'])

    async def test_replay_and_repeated_hold_do_not_duplicate_history(self):
        await self.flow.process(self.message(), self.sender, 900)
        await self.flow.process(self.message(), self.sender, 900)
        await self.flow.process(self.message(message_id=201), self.sender, 900)
        self.assertEqual(self.store.hold_request_count(), 1)

    async def test_ambiguous_hold_is_saved_without_guessing_price(self):
        other = replace(self.quoted, name='Galaxy S26 12/512 Blue SM-S942B/DS', price=Decimal('65000'))
        self.store.save_quote(77, 88, -123, 29, 's26', [self.quoted, other], now=1001, sent_message_ids=[901])
        self.assertEqual(await self.flow.process(self.message('отложите'), self.sender, 901), 'clarify')
        row = self.store.hold_requests()[0]
        self.assertEqual(row['kind'], 'clarify')
        self.assertEqual(row['product_text'], '')
        self.assertEqual(row['buyer_text'], 'отложите')

    async def test_notice_failure_does_not_lose_journal_entry(self):
        self.notify.return_value = False
        self.assertEqual(await self.flow.process(self.message(), self.sender, 900), 'notice_failed')
        self.assertEqual(self.store.hold_request_count(), 1)

    async def test_journal_failure_rolls_back_stock_and_followup(self):
        item = replace(self.product, stock=3)
        self.store.replace_products([item])
        self.store.save_quote(77, 88, -123, 29, 's26 black', [item], now=1001, sent_message_ids=[901])
        self.store.db.execute('''CREATE TRIGGER fail_hold_journal BEFORE INSERT ON hold_requests
                                 BEGIN SELECT RAISE(ABORT, 'journal unavailable'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            await self.flow.process(self.message(), self.sender, 901)
        self.assertEqual(self.store.products()[0].stock, 3)
        self.assertIsNone(self.store.followup(77, 88, 200))
        self.assertEqual(self.store.stock_reservations(), [])

    async def test_quoted_price_is_kept_in_the_journal_for_long_names(self):
        item = replace(self.product, name='Galaxy S26 12/256 Black ' + 'X' * 190, price=Decimal('57500'))
        self.store.save_quote(77, 88, -123, 29, 's26 black', [item], now=1001, sent_message_ids=[901])
        await self.flow.process(self.message(), self.sender, 901)
        transport = LocalSession()
        bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=transport)
        app = Application(Config(123, 'test', '', 55, Path(self.tmp.name)), self.store, LocalClient(), bot)
        message = SimpleNamespace(answer=AsyncMock())
        try:
            await app.show_holds(message)
            self.assertIn('57 500 ₽', message.answer.await_args.args[0])
        finally:
            await bot.session.close()

    async def test_menu_renders_tag_price_and_no_false_stock_claim(self):
        await self.flow.process(self.message(), self.sender, 900)
        transport = LocalSession()
        bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=transport)
        app = Application(Config(123, 'test', '', 55, Path(self.tmp.name)), self.store, LocalClient(), bot)
        message = SimpleNamespace(answer=AsyncMock())
        try:
            await app.show_holds(message)
            text = message.answer.await_args.args[0]
            self.assertIn('@buyer_test', text)
            self.assertIn('57 500 ₽', text)
            self.assertIn('Остаток не списан', text)
            self.assertEqual(message.answer.await_args.kwargs['parse_mode'], 'HTML')
            labels = {button.callback_data: button.text for row in menu().inline_keyboard for button in row}
            self.assertEqual(labels['holds:0'], '📌 Отложки')
        finally:
            await bot.session.close()

    async def test_name_without_username_is_escaped_and_clickable(self):
        self.sender.username = None
        self.sender.first_name = '<Илья &>'
        await self.flow.process(self.message(), self.sender, 900)
        transport = LocalSession()
        bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=transport)
        app = Application(Config(123, 'test', '', 55, Path(self.tmp.name)), self.store, LocalClient(), bot)
        message = SimpleNamespace(answer=AsyncMock())
        try:
            await app.show_holds(message)
            text = message.answer.await_args.args[0]
            self.assertIn('tg://user?id=88', text)
            self.assertIn('&lt;Илья &amp;&gt;', text)
        finally:
            await bot.session.close()

    async def test_holds_command_is_accessible_only_to_owner(self):
        await self.flow.process(self.message(), self.sender, 900)
        transport = LocalSession()
        bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=transport)
        app = Application(Config(123, 'test', '', 55, Path(self.tmp.name)), self.store, LocalClient(), bot)
        try:
            for number, user in enumerate((66, 55), 1):
                msg = Message(message_id=number, date=datetime.now(timezone.utc),
                              chat=Chat(id=user, type='private'),
                              from_user=User(id=user, is_bot=False, first_name='User'), text='/holds')
                await app.dp.feed_update(bot, Update(update_id=number, message=msg))
            sent = [call for call in transport.calls if call.__api_method__ == 'sendMessage']
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0].chat_id, 55)
            self.assertIn('57 500 ₽', sent[0].text)
        finally:
            await bot.session.close()
