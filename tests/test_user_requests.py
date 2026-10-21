import unittest
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import build_reply, find_products
from catalog_bot.prices import parse_price_feed


USER_REQUEST = '''OnePlus 15R 12/512Gb Mint Breeze
OnePlus 15R 12/256Gb Mint Breeze
17 Pro Max 256 синий 1 сим — 59
Honor 600 Lite 8/256 Gold - 1
Samsung Galaxy S26 ULtra 512gb Black'''


class UserRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent.parent / 'examples' / 'request_price_excerpt.txt'
        cls.products = list(parse_price_feed(path.read_text(), require_complete=True).products)

    def assert_samsung_quote(self, products):
        self.assertEqual(len(products), 2)
        self.assertEqual({p.name for p in products}, {'Galaxy S26 Ultra 12/512 Black SM-S948B/DS'})
        self.assertEqual({p.country for p in products}, {'🇿🇦', '🇨🇱'})
        self.assertEqual({p.price for p in products}, {Decimal('91500')})
        self.assertTrue(all(p.stock is None for p in products))

    def test_user_queries_do_not_substitute_neighboring_models(self):
        absent = USER_REQUEST.splitlines()[:-1] + ['18 Pro 256GB Glacier 1sim']
        for query in absent:
            with self.subTest(query=query):
                self.assertEqual(find_products(query, self.products)[1], [])
        self.assert_samsung_quote(find_products(USER_REQUEST.splitlines()[-1], self.products)[1])

    def test_multiline_bare_request_quotes_same_products_as_single_lines(self):
        classification, products = find_products(USER_REQUEST, self.products)
        self.assertEqual(classification, 'bare')
        self.assert_samsung_quote(products)
        reply = build_reply(products)
        self.assertEqual(reply.count('91 500 ₽'), 2)
        self.assertIn('По актуальному прайсу:', reply)
        for absent in ('OnePlus', 'Honor', 'iPhone', '76 700', '113 300'):
            self.assertNotIn(absent, reply)

    def test_multiline_support_preserves_intent_guards(self):
        self.assertEqual(find_products(USER_REQUEST, self.products, allow_bare=False)[1], [])
        self.assert_samsung_quote(find_products('Куплю\n' + USER_REQUEST, self.products, allow_bare=False)[1])
        self.assertEqual(find_products(USER_REQUEST + '\nПродам iPhone 17', self.products)[1], [])
        self.assertEqual(find_products(' '.join(USER_REQUEST.splitlines()), self.products)[1], [])

    def test_user_samsung_typo_and_cyrillic_series_quote_same_price(self):
        for query in ('сасунг с26 ультра 512 черный', 'Самсунг С26 Ultra 512GB Black',
                      'галакси с26 ультра 512 черный'):
            with self.subTest(query=query):
                self.assert_samsung_quote(find_products(query, self.products)[1])

    def test_samsung_alias_keeps_model_version_memory_and_color_constraints(self):
        for query in ('сасунг с25 ультра 512 черный', 'сасунг с26 512 черный'):
            with self.subTest(query=query):
                self.assertEqual(find_products(query, self.products)[1], [])
        found = find_products('сасунг с26 ультра 256 черный', self.products)[1]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].price, Decimal('76700'))
        found = find_products('сасунг с26 ультра 512 белый', self.products)[1]
        self.assertEqual(len(found), 1)
        self.assertIn(' White ', found[0].name)
