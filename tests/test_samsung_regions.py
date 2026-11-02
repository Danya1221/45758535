import unittest
from decimal import Decimal

from catalog_bot.catalog import Product, find_products
from catalog_bot.offer_selection import select_offers


def product(code, region, price, extra=''):
    return Product(f'Galaxy S26 12/256 Cobalt Violet {code} {extra}'.strip(), Decimal(str(price)),
                   None, country=region, price_country=region)


class SamsungRegionTests(unittest.TestCase):
    def setUp(self):
        self.za = product('SM-S942B/DS', '🇿🇦', 57300)
        self.kz = product('SM-S942B/DS', '🇰🇿', 58200)
        self.hk = product('SM-S9420', '🇭🇰', 56700)
        self.cn = product('SM-S9420', '🇨🇳', 56000, 'Snapdragon')
        self.products = [self.za, self.kz, self.hk, self.cn]

    def matches(self, query):
        _, products = find_products(query, self.products, regional_policy=True)
        return select_offers(products)

    def test_default_and_explicit_global_choose_cheapest_global(self):
        for suffix in ('', 'глобал', 'global'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.matches('s26 256 cobalt violet ' + suffix), [self.za])

    def test_explicit_hong_kong_and_china(self):
        for suffix, expected in [('конг', self.hk), ('гонконг', self.hk), ('Hong Kong', self.hk),
                                 ('🇭🇰', self.hk), ('китай', self.cn), ('китайская версия', self.cn),
                                 ('china', self.cn), ('🇨🇳', self.cn)]:
            with self.subTest(suffix=suffix):
                self.assertEqual(self.matches('s26 256 cobalt violet ' + suffix), [expected])

    def test_snapdragon_is_not_guessed_from_country_or_model_code(self):
        self.assertEqual(self.matches('s26 256 cobalt violet Snapdragon'), [self.cn])
        self.assertEqual(self.matches('s26 256 cobalt violet на снепе'), [self.cn])
        self.assertEqual(self.matches('s26 256 cobalt violet Snapdragon конг'), [])
        self.products = [self.hk]
        self.assertEqual(self.matches('s26 256 cobalt violet Snapdragon'), [])

    def test_unknown_version_and_contradictory_request_fail_closed(self):
        unknown = product('', '', 50000)
        self.products = [unknown]
        self.assertEqual(self.matches('s26 256 cobalt violet'), [])
        self.products = [self.za, self.hk]
        self.assertEqual(self.matches('s26 256 cobalt violet global конг'), [])

    def test_china_in_product_name_does_not_pass_as_global(self):
        contradictory = product('SM-S942B/DS', '', 50000, 'China')
        self.products = [contradictory]
        self.assertEqual(self.matches('s26 256 cobalt violet'), [])

    def test_explicit_supplier_code_can_select_requested_hk_model(self):
        self.assertEqual(self.matches('SM-S9420'), [self.hk, self.cn])

    def test_unrelated_brand_version_words_are_not_removed(self):
        items = [Product('iPhone 17 256 Black', Decimal('50000'), None, country='🇨🇳')]
        self.assertEqual(find_products('iPhone 17 256 Black global', items, regional_policy=True)[1], [])
