import unittest
from decimal import Decimal

from catalog_bot.catalog import Product, find_products


class IcyBlueMatchingTests(unittest.TestCase):
    def setUp(self):
        self.icy = Product('Galaxy S25 12/256 Icyblue SM-S931B/DS', Decimal('49800'), None, country='🇮🇳')
        self.blue = Product('Galaxy S25 12/256 Blue SM-S931B/DS', Decimal('50000'), None, country='🇮🇳')
        self.navy = Product('Galaxy S25 12/256 Navy SM-S931B', Decimal('50800'), None, country='🇮🇳')
        self.edge = Product('Galaxy S25 Edge 12/256 Icyblue SM-S937B/DS', Decimal('49000'), None, country='🇪🇺')
        self.ultra = Product('Galaxy S25 Ultra 12/256 Icyblue SM-S938B/DS', Decimal('66500'), None, country='🇦🇪')
        self.other = Product('Xiaomi 25 12/256 Icy Blue', Decimal('30000'), None, country='🇪🇺')
        self.products = [self.icy, self.blue, self.navy, self.edge, self.ultra, self.other]

    def matches(self, text):
        return set(find_products(text, self.products)[1])

    def test_general_blue_includes_icy_but_not_navy_or_other_models(self):
        for text in ('S25 Blue', 's25 синий', 'Samsung S25 голубой'):
            with self.subTest(text=text):
                self.assertEqual(self.matches(text), {self.icy, self.blue})

    def test_explicit_icy_shade_does_not_match_generic_blue_or_navy(self):
        for text in ('S25 Icyblue', 'Samsung S25 Icy Blue', 's25 ледяной голубой'):
            with self.subTest(text=text):
                self.assertEqual(self.matches(text), {self.icy})

    def test_memory_region_and_variant_are_not_relaxed(self):
        self.assertEqual(self.matches('S25 Blue 512'), set())
        self.assertEqual(self.matches('S25 Blue 🇪🇺'), set())
        self.assertEqual(self.matches('S25 Edge Blue'), {self.edge})
        self.assertEqual(self.matches('S25 Ultra Icyblue'), {self.ultra})

    def test_other_joined_shades_match_only_appropriate_general_colors(self):
        jet = Product('Galaxy S25 FE 8/256 Jetblack SM-S731B/DS', Decimal('43000'), None)
        ultra = Product('Galaxy S25 Ultra 12/512 Whitesilver SM-S938B/DS', Decimal('71700'), None)
        plain = Product('Galaxy S25 FE 8/256 Black SM-S731B/DS', Decimal('44000'), None)
        products = [jet, ultra, plain]
        self.assertEqual(set(find_products('S25 FE Black', products)[1]), {jet, plain})
        self.assertEqual(set(find_products('S25 FE Jetblack', products)[1]), {jet})
        self.assertEqual(find_products('S25 Ultra White', products)[1], [ultra])
        self.assertEqual(find_products('S25 Ultra Silver', products)[1], [ultra])
