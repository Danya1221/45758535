import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import find_products, parse_catalog, split_inline_price
from catalog_bot.prices import IncompletePrice, PriceFeedError, parse_price_feed
from catalog_bot.storage import Store


TEXT = '''Apple iPhone 18 Pro Max eSIM 512Gb Burgundi 🇺🇸 138 000

Apple iPhone 18 Pro Max eSIM 256Gb Silver 🇺🇸 119 000
Apple iPhone 18 Pro eSIM 256Gb Glasier 🇺🇸 121 500

Apple iPhone 18 Pro eSIM 256Gb Burgundy 🇺🇸 105 500'''
BOT_PRICE = '• Sony WH-1000XM6 Black🇪🇺\n🇪🇺 От 1 шт - 25 000\nВсего найдено 1 товаров'


class InlinePriceTests(unittest.TestCase):
    def test_screenshot_format_for_both_imports(self):
        for products in (list(parse_price_feed(TEXT).products), parse_catalog(TEXT)):
            self.assertEqual([p.price for p in products], list(map(Decimal, ['138000', '119000', '121500', '105500'])))
            self.assertEqual([p.country for p in products], ['🇺🇸'] * 4)
            self.assertTrue(all(p.stock is None for p in products))
            self.assertIn('512Gb Burgundi', products[0].name)
        self.assertFalse(parse_price_feed(TEXT).complete)

    def test_footer_and_duplicate_validation(self):
        self.assertTrue(parse_price_feed(TEXT + '\nВсего найдено 4 товаров', require_complete=True).complete)
        with self.assertRaises(IncompletePrice):
            parse_price_feed(TEXT + '\nВсего найдено 5 товаров')
        with self.assertRaises(PriceFeedError):
            parse_price_feed('Apple iPhone 18 Black 🇺🇸 138 000\nApple iPhone 18 Black 🇺🇸 139 000')

    def test_memory_and_quantity_never_become_a_price(self):
        for text in ['iPhone 18 Pro 256', 'iPhone 18 Pro Black - 1', 'iPhone 18 Pro 512 138 000']:
            with self.subTest(text=text):
                self.assertIsNone(split_inline_price(text))
                with self.assertRaises(PriceFeedError):
                    parse_price_feed(text)

    def test_different_regions_are_distinct(self):
        products = parse_catalog('iPhone 18 Black 🇺🇸 100 000\niPhone 18 Black 🇨🇦 101 000')
        self.assertEqual([p.country for p in products], ['🇺🇸', '🇨🇦'])

    def test_original_color_typos_match_correct_spelling_without_crossing_colors(self):
        products = list(parse_price_feed(TEXT).products)
        _, burgundy = find_products('iPhone 18 Pro Max 512 Burgundy esim', products)
        _, glacier = find_products('iPhone 18 Pro 256 Glacier esim', products)
        self.assertEqual([p.price for p in burgundy], [Decimal('138000')])
        self.assertEqual([p.price for p in glacier], [Decimal('121500')])

    def test_inline_manual_price_keeps_auto_supplier_and_own_goods(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'db.sqlite3')
            store.replace_products(parse_catalog('iPhone 17|90000|2'))
            store.apply_price_snapshot(parse_price_feed(BOT_PRICE), 'bot:123')
            store.apply_price_snapshot(parse_price_feed(TEXT), 'manual:price')
            self.assertEqual(len(store._base_products()), 1)
            self.assertEqual(len(store._supplier_products()), 1)
            self.assertEqual(len(store._manual_price_products()), 4)
            store.apply_price_snapshot(parse_price_feed(BOT_PRICE.replace('25 000', '26 000')), 'bot:123')
            self.assertEqual(len(store._manual_price_products()), 4)
            store.apply_price_snapshot(parse_price_feed(TEXT.splitlines()[0].replace('138 000', '139 000')), 'manual:price')
            self.assertEqual(len(store._manual_price_products()), 4)
            self.assertEqual(store._manual_price_products()[0].price, Decimal('139000'))
            store.toggle_answer_source('price')
            self.assertEqual(len(store.quotable_products()), 1)
            store.close()
            store = Store(Path(tmp) / 'db.sqlite3')
            self.assertEqual(len(store._manual_price_products()), 4)
            self.assertFalse(store.source_enabled('price'))
            store.close()

    def test_previous_manual_price_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db.sqlite3'
            store = Store(path)
            store.apply_price_snapshot(parse_price_feed(BOT_PRICE), 'bot:123')
            store.set('price_source', 'manual:price')
            store.set('manual_price_split_v1', False)
            store.close()
            store = Store(path)
            self.assertEqual(len(store._manual_price_products()), 1)
            self.assertEqual(store._supplier_products(), [])
            store.close()
