import tempfile
import unittest
import sqlite3
from unittest.mock import patch
from pathlib import Path

from catalog_bot.catalog import parse_catalog
from catalog_bot.prices import parse_price_feed
from catalog_bot.storage import Store
from catalog_bot.engine import Engine, Incoming


class DeletePersonalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db.sqlite')
        self.store.replace_products(parse_catalog('iPhone 17 Black|85000|2\niPhone 17 Silver|86000|3'))
        self.store.apply_price_snapshot(parse_price_feed('iPhone 18 Black 🇺🇸 100 000 9шт'), 'manual:price')
        self.store.apply_price_snapshot(parse_price_feed('• Sony WH-1000XM6 Black🇪🇺\n🇪🇺 От 1 шт - 25 000\nВсего найдено 1 товаров'), 'bot:123')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_delete_only_selected_source_and_keeps_other_sources(self):
        own = self.store.personal_items()[0]
        version = self.store.get('catalog_version')
        self.assertEqual(self.store.delete_personal_item(*own[:3]), 'deleted')
        self.assertGreater(self.store.get('catalog_version'), version)
        self.assertEqual([p.name for p in self.store._base_products()], ['iPhone 17 Silver'])
        self.assertEqual(len(self.store._manual_price_products()), 1)
        self.assertEqual(len(self.store._supplier_products()), 1)
        self.assertEqual(self.store.delete_personal_item(*own[:3]), 'stale')

    def test_stale_button_cannot_delete_reimported_item(self):
        old = next(item for item in self.store.personal_items() if item[0] == 'm')
        self.store.apply_price_snapshot(parse_price_feed('iPhone 18 Black 🇺🇸 101 000 5шт'), 'manual:price')
        self.assertEqual(self.store.delete_personal_item(*old[:3]), 'stale')
        self.assertEqual(str(self.store._manual_price_products()[0].price), '101000')

    def test_reserved_item_requires_release_before_deletion(self):
        item = next(item for item in self.store.personal_items() if item[0] == 'm')
        product = item[3]
        quote = self.store.save_quote(77, 88, -123, 1, 'iphone', [product], now=1000)
        reservation = self.store.reserve_stock_followup(77, 88, 2, quote, product, 1, 1001)
        self.assertEqual(reservation['status'], 'reserved')
        self.assertEqual(self.store.delete_personal_item(*item[:3]), 'reserved')
        self.assertEqual(self.store.release_stock_reservation(reservation['reservation_id']), 'released')
        self.assertEqual(self.store.delete_personal_item(*item[:3]), 'deleted')

    def test_deleting_different_item_keeps_existing_reservation_releasable(self):
        first, second = self.store.personal_items()[:2]
        product = first[3]
        quote = self.store.save_quote(77, 88, -123, 1, 'iphone', [product], now=1000)
        reservation = self.store.reserve_stock_followup(77, 88, 2, quote, product, 1, 1001)
        self.assertEqual(self.store.delete_personal_item(*second[:3]), 'deleted')
        self.assertEqual(self.store.release_stock_reservation(reservation['reservation_id']), 'released')
        self.assertEqual(self.store._base_products()[0].stock, 2)

    def test_unknown_source_cannot_delete(self):
        self.assertEqual(self.store.delete_personal_item('supplier', 1, 0), 'stale')
        self.assertEqual(len(self.store._supplier_products()), 1)

    def test_delete_failure_rolls_back_entire_change(self):
        item = self.store.personal_items()[0]
        self.store.db.execute('''CREATE TRIGGER fail_delete_version BEFORE INSERT ON settings
            WHEN NEW.key='catalog_version' BEGIN SELECT RAISE(ABORT, 'failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_personal_item(*item[:3])
        self.assertIsNotNone(self.store.personal_item(*item[:3]))


class DeleteDuringReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletion_during_delay_cancels_sending_old_offer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'db.sqlite')
            store.replace_products(parse_catalog('iPhone 17 Black|85000|2'))
            store.toggle_chat(-123, 'test')
            store.set('active', True)
            sent = []
            async def send(*args):
                sent.append(args)
            engine = Engine(store, send, clock=lambda: 1000, reply_delay_seconds=10)
            async def delete(_):
                self.assertEqual(store.delete_personal_item(*store.personal_items()[0][:3]), 'deleted')
            with patch('catalog_bot.engine.asyncio.sleep', side_effect=delete):
                result = await engine.process(Incoming(-123, 1, 88, 'iPhone 17 Black', 1000))
            self.assertEqual(result, 'cancelled')
            self.assertEqual(sent, [])
            store.close()
