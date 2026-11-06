import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_bot.catalog import parse_catalog
from catalog_bot.engine import Engine, Incoming
from catalog_bot.prices import parse_price_feed
from catalog_bot.storage import Store


FEED = '• iPhone 17 256 Black SIM🇪🇺\n🇪🇺 От 1 шт - 85 000\nВсего найдено 1 товаров'


class AnswerSourcesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db.sqlite'
        self.store = Store(self.path)
        self.store.replace_products(parse_catalog('iPhone 17 256 Black SIM|90000|4'))
        self.store.apply_price_snapshot(parse_price_feed(FEED), 'bot:100')
        self.store.toggle_chat(-123, 'test')
        self.store.set('active', True)
        self.sent = []
        async def send(user, message, text):
            self.sent.append(text)
            return []
        self.engine = Engine(self.store, send, dedup_seconds=0, clock=lambda: 1000)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def request(self, n):
        return await self.engine.process(Incoming(-123, n, n, 'iPhone 17 256 Black SIM', 1000))

    async def test_independent_toggles_and_persistence(self):
        self.assertEqual(await self.request(1), 'sent')
        self.assertIn('85 000 ₽', self.sent[-1])
        self.store.toggle_answer_source('price')
        self.assertEqual(await self.request(2), 'sent')
        self.assertIn('90 000 ₽', self.sent[-1])
        self.assertNotIn('85 000 ₽', self.sent[-1])
        self.store.toggle_answer_source('own')
        self.assertEqual(await self.request(3), 'no_match')
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.source_enabled('own'))
        self.assertFalse(self.store.source_enabled('price'))
        self.assertEqual(len(self.store.products()), 2)
        self.engine.store = self.store
        self.store.toggle_answer_source('price')
        self.assertEqual(await self.request(4), 'sent')
        self.assertIn('85 000 ₽', self.sent[-1])

    async def test_supplier_failure_keeps_own_items_available(self):
        self.store.set('price_ready', False)
        self.assertEqual(await self.request(1), 'sent')
        self.assertIn('90 000 ₽', self.sent[-1])
        self.store.toggle_answer_source('own')
        self.assertEqual(await self.request(2), 'price_unavailable')

    async def test_updates_are_independent(self):
        self.store.replace_products(parse_catalog('iPhone 17 256 Black SIM|95000|3'))
        self.assertEqual(len(self.store.products()), 2)
        self.store.apply_price_snapshot(parse_price_feed(FEED.replace('85 000', '86 000')), 'bot:100')
        self.assertEqual([(p.source, int(p.price)) for p in self.store.products()], [('', 95000), ('supplier', 86000)])

    async def test_disabling_price_during_delay_cancels_send(self):
        self.engine.reply_delay_seconds = 10
        async def change(_):
            self.store.toggle_answer_source('price')
        with patch('catalog_bot.engine.asyncio.sleep', side_effect=change):
            self.assertEqual(await self.request(1), 'cancelled')
        self.assertEqual(self.sent, [])

    async def test_supplier_update_does_not_break_own_stock_reservation(self):
        own = self.store._base_products()[0]
        quote = self.store.save_quote(77, 88, -123, 1, 'iPhone', [own], now=1000)
        self.store.apply_price_snapshot(parse_price_feed(FEED.replace('85 000', '86 000')), 'bot:100')
        result = self.store.reserve_stock_followup(77, 88, 50, quote, own, 2, 1001)
        self.assertEqual(result['status'], 'reserved')
        self.assertEqual(self.store._base_products()[0].stock, 2)
        self.assertEqual(self.store.release_stock_reservation(result['reservation_id']), 'released')
        self.assertEqual(self.store._base_products()[0].stock, 4)

    async def test_migration_keeps_legacy_supplier_and_does_not_reclassify_it(self):
        self.store.db.execute('DELETE FROM products')
        self.store.db.execute('INSERT INTO products(name,price,stock,sku,stock_known,country,price_country,min_qty,warnings) '
                              "VALUES ('old supplier','123',1,'',0,'','','1','[]')")
        self.store.db.execute('DELETE FROM supplier_products')
        self.store.set('price_source', 'bot:100')
        self.store.set('source_split_v1', False)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store._base_products(), [])
        self.assertEqual([p.name for p in self.store._supplier_products()], ['old supplier'])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(len(self.store._supplier_products()), 1)

    async def test_migration_keeps_legacy_own_goods_and_stock(self):
        self.store.db.execute('DELETE FROM supplier_products')
        self.store.set('price_source', None)
        self.store.set('source_split_v1', False)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store._base_products()[0].stock, 4)
        self.assertEqual(self.store._supplier_products(), [])
