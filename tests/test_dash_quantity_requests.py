import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import Product, find_products, inspect_request, parse_catalog
from catalog_bot.engine import Engine, Incoming
from catalog_bot.storage import Store


QUERY = ('S26 Ultra 256 White - 1\n'
         'S26 Ultra 512 Black - 3\n'
         'S26 Ultra 512 Violet - 1\n'
         'S26 Ultra 512 White - 1')


def product(name, price, stock, *, country='🇨🇱', sku=''):
    return Product(name, Decimal(str(price)), stock, sku=sku, country=country)


class DashQuantityTests(unittest.TestCase):
    def setUp(self):
        self.white256 = product('Galaxy S26 Ultra 12/256 White SM-S948B/DS', 77000, 1)
        self.black512 = product('Galaxy S26 Ultra 12/512 Black SM-S948B/DS', 91500, 8)
        self.white512 = product('Galaxy S26 Ultra 12/512 White SM-S948B/DS', 91600, 41)
        self.not_enough = product('Galaxy S26 Ultra 12/512 Black SM-S948B/DS', 90000, 2, sku='FEW')
        self.hong_kong = product('Galaxy S26 Ultra 12/512 Black SM-S9480', 89000, 9, country='🇭🇰')
        self.items = [self.white256, self.black512, self.white512, self.not_enough, self.hong_kong,
                      product('Galaxy S26 Ultra 12/256 Sky Blue SM-S948B/DS', 76000, 30),
                      product('Galaxy S26 12/512 Black SM-S942B/DS', 70000, 20),
                      product('iPhone 18 Pro Max 512 Black', 100000, 20)]

    def matches(self, query):
        return find_products(query, self.items, regional_policy=True)[1]

    def test_screenshot_multiline_matches_each_model_memory_and_color(self):
        report = inspect_request(QUERY, self.items, regional_policy=True)
        self.assertEqual(report.classification, 'bare')
        self.assertEqual(report.products, [self.white256, self.black512, self.white512])
        self.assertNotIn('неясное число', '\n'.join(report.details))
        self.assertIn('Запрошено: 3 шт.', report.details)

    def test_single_line_and_typographic_dashes_work(self):
        for dash in ('-', '–', '—'):
            with self.subTest(dash=dash):
                self.assertEqual(self.matches(f'S26 Ultra 512 Black {dash} 3'), [self.black512])

    def test_known_stock_must_cover_quantity_but_unknown_is_not_guessed(self):
        self.assertEqual(self.matches('S26 Ultra 512 Black - 9'), [])
        unknown = product('Galaxy S26 Ultra 12/512 Black SM-S948B/DS', 92000, None)
        self.items = [unknown]
        self.assertEqual(self.matches('S26 Ultra 512 Black - 3'), [unknown])
        report = inspect_request('S26 Ultra 512 Black - 3', self.items, regional_policy=True)
        self.assertIn('Остаток не указан, количество нужно подтвердить.', report.details)

    def test_buyer_header_respected_and_sales_are_not_answered(self):
        self.assertEqual(find_products('Куплю\n' + QUERY, self.items, allow_bare=False,
                                       regional_policy=True)[1], [self.white256, self.black512, self.white512])
        for text in ('Продам\n' + QUERY, 'S26 Ultra 512 Black - 91 500 ₽',
                     'S26 Ultra 512 Black - 59', 'S26 Ultra 512 Black - 0'):
            with self.subTest(text=text):
                self.assertEqual(self.matches(text), [])

    def test_two_digit_quantity_requires_unit(self):
        self.assertEqual(self.matches('S26 Ultra 512 White - 15'), [])
        self.assertEqual(self.matches('S26 Ultra 512 White - 15 шт'), [self.white512])
        self.assertEqual(self.matches('S26 Ultra 512 White - 50 шт'), [])

    def test_blank_manual_stock_is_unknown_not_an_invented_one(self):
        name = 'Galaxy S26 Ultra 12/512 Black SM-S948B/DS'
        unknown = parse_catalog(name + '|91500')[0]
        self.assertIsNone(unknown.stock)
        self.assertEqual(find_products('S26 Ultra 512 Black - 3', [unknown], regional_policy=True)[1], [unknown])
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.sqlite3')
            try:
                store.replace_products([unknown])
                self.assertIsNone(store.products()[0].stock)
            finally:
                store.close()
        only_one = parse_catalog(name + '|91500|1')[0]
        self.assertEqual(only_one.stock, 1)
        self.assertEqual(find_products('S26 Ultra 512 Black - 3', [only_one], regional_policy=True)[1], [])


class DashQuantityEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_contains_only_available_requested_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.sqlite3')
            try:
                items = DashQuantityTests()
                items.setUp()
                store.replace_products(items.items)
                store.toggle_chat(-123, 'test')
                store.set('active', True)
                sent = []

                async def send(recipient, message_id, text):
                    sent.append((recipient, message_id, text))

                engine = Engine(store, send, clock=lambda: 1000)
                self.assertEqual(await engine.process(Incoming(-123, 1, 55, QUERY, 1000)), 'sent')
                self.assertEqual(sent[0][:2], (55, 1))
                reply = sent[0][2]
                self.assertIn('12/256 White', reply)
                self.assertIn('12/512 Black', reply)
                self.assertIn('12/512 White', reply)
                for incorrect in ('Violet', 'Sky Blue', 'iPhone', '🇭🇰', '90 000'):
                    self.assertNotIn(incorrect, reply)
            finally:
                store.close()
