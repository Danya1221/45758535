"""Stock changes through a buyer's private follow-up, without a buyer auto-reply."""
import asyncio
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from catalog_bot.catalog import Product, build_reply
from catalog_bot.conversations import ConversationReplies
from catalog_bot.engine import Incoming
from catalog_bot.storage import Store


class StockReservationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'bot.sqlite3')
        self.store.set('telegram_account_id', 77)
        self.store.set('active', True)
        self.sender = SimpleNamespace(id=88, username='buyer_test', first_name='Buyer', last_name=None)
        self.send = AsyncMock(return_value=[901])
        self.notify = AsyncMock(return_value=True)
        self.flow = ConversationReplies(self.store, self.send, self.notify, asyncio.Lock(), clock=lambda: 1001)
        self.item = Product('Galaxy S25 12/256 Black', Decimal('49000'), 5, sku='s25-black')
        self.other = Product('Galaxy S25 12/256 Navy', Decimal('50000'), 4, sku='s25-navy')

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def quote(self, products, *, sent_id=900, group_message=28, buyer=88):
        return self.store.save_quote(77, buyer, -123, group_message, 's25', products,
                                     now=1000, sent_message_ids=[sent_id])

    def message(self, text, *, message_id=200, buyer=88):
        return Incoming(chat_id=buyer, message_id=message_id, sender_id=buyer,
                        text=text, timestamp=1001, is_group=False)

    def stocks(self):
        return {p.sku: p.stock for p in self.store.products()}

    async def test_hold_verbs_decrement_one_and_notify_owner_without_buyer_reply(self):
        for index, text in enumerate(('отложи', 'отложите', 'закиньте')):
            with self.subTest(text=text):
                self.store.replace_products([self.item])
                self.quote([self.item], sent_id=900 + index, group_message=28 + index)
                self.notify.reset_mock()
                await self.flow.process(self.message(text, message_id=200 + index), self.sender, 900 + index)
                self.assertEqual(self.stocks()['s25-black'], 4)
                self.notify.assert_awaited_once()
                self.assertIn('@buyer_test', self.notify.await_args.args[0])
                self.send.assert_not_awaited()

    async def test_explicit_quantity_and_quoted_offer_are_used(self):
        for index, text in enumerate(('отложите 2 шт', 'закиньте 2')):
            with self.subTest(text=text):
                self.store.replace_products([self.item, self.other])
                self.quote([self.item, self.other], sent_id=900 + index, group_message=28 + index)
                self.notify.reset_mock()
                await self.flow.process(self.message(text, message_id=200 + index), self.sender, 900 + index,
                                        quoted_text=build_reply([self.item], intro=''))
                self.assertEqual(self.stocks(), {'s25-black': 3, 's25-navy': 4})
                self.notify.assert_awaited_once()
                self.send.assert_not_awaited()

    async def test_ambiguous_offer_does_not_decrement(self):
        self.store.replace_products([self.item, self.other])
        self.quote([self.item, self.other])
        await self.flow.process(self.message('отложите'), self.sender, 900)
        self.assertEqual(self.stocks(), {'s25-black': 5, 's25-navy': 4})
        self.notify.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_reimported_catalog_invalidates_old_quote(self):
        self.store.replace_products([self.item])
        self.quote([self.item])
        self.store.replace_products([replace(self.item, stock=8)])
        await self.flow.process(self.message('отложите'), self.sender, 900)
        self.assertEqual(self.stocks()['s25-black'], 8)
        self.notify.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_duplicate_message_and_repeated_hold_do_not_decrement_again(self):
        self.store.replace_products([self.item])
        self.quote([self.item])
        await self.flow.process(self.message('отложи'), self.sender, 900)
        await self.flow.process(self.message('отложи'), self.sender, 900)
        await self.flow.process(self.message('закиньте', message_id=201), self.sender, 900)
        self.assertEqual(self.stocks()['s25-black'], 4)
        self.notify.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_stock_change_does_not_invalidate_other_buyers_existing_quote(self):
        self.store.replace_products([self.item])
        self.quote([self.item])
        self.quote([self.item], sent_id=910, group_message=29, buyer=89)
        second = SimpleNamespace(id=89, username='second', first_name='Second', last_name=None)
        await self.flow.process(self.message('отложи'), self.sender, 900)
        await self.flow.process(self.message('отложите', message_id=201, buyer=89), second, 910)
        self.assertEqual(self.stocks()['s25-black'], 3)
        self.assertEqual(self.notify.await_count, 2)
        self.send.assert_not_awaited()

    async def test_insufficient_or_unknown_stock_is_not_decremented(self):
        for index, stock in enumerate((1, None)):
            with self.subTest(stock=stock):
                item = replace(self.item, stock=stock)
                self.store.replace_products([item])
                self.quote([item], sent_id=900 + index, group_message=28 + index)
                self.notify.reset_mock()
                await self.flow.process(self.message('отложите 2 шт', message_id=200 + index),
                                        self.sender, 900 + index)
                self.assertEqual(self.stocks()['s25-black'], stock)
                self.notify.assert_awaited_once()
                self.send.assert_not_awaited()

    async def test_not_a_reservation_does_not_change_stock(self):
        self.store.replace_products([self.item])
        self.quote([self.item])
        for index, text in enumerate(('не откладывайте', 'закиньте прайс', 'закиньте фото', '-200'), 200):
            with self.subTest(text=text):
                await self.flow.process(self.message(text, message_id=index), self.sender, 900)
                self.assertEqual(self.stocks()['s25-black'], 5)
        self.send.assert_not_awaited()
        self.notify.assert_not_awaited()

    async def test_release_once_restores_stock(self):
        self.store.replace_products([self.item])
        self.quote([self.item])
        await self.flow.process(self.message('отложи 2 шт'), self.sender, 900)
        reservation = self.store.stock_reservations()[0]
        self.assertEqual(self.stocks()['s25-black'], 3)
        self.assertEqual(self.store.release_stock_reservation(reservation['id']), 'released')
        self.assertEqual(self.stocks()['s25-black'], 5)
        self.assertEqual(self.store.release_stock_reservation(reservation['id']), 'missing')
        self.assertEqual(self.stocks()['s25-black'], 5)

    async def test_release_does_not_change_a_replaced_catalog(self):
        self.store.replace_products([self.item])
        self.quote([self.item])
        await self.flow.process(self.message('отложи'), self.sender, 900)
        reservation = self.store.stock_reservations()[0]
        self.store.replace_products([replace(self.item, stock=8)])
        self.assertEqual(self.store.release_stock_reservation(reservation['id']), 'changed')
        self.assertEqual(self.stocks()['s25-black'], 8)

    async def test_restart_does_not_deduct_a_second_time(self):
        self.store.replace_products([self.item])
        self.quote([self.item])
        await self.flow.process(self.message('отложи'), self.sender, 900)
        path = Path(self.temp.name) / 'bot.sqlite3'
        self.store.close()
        self.store = Store(path)
        self.flow.store = self.store
        await self.flow.process(self.message('отложи', message_id=201), self.sender, 900)
        self.assertEqual(self.stocks()['s25-black'], 4)


if __name__ == '__main__':
    unittest.main()
