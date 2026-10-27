"""Broad model requests must enumerate variants without crossing identities."""
import unittest
from decimal import Decimal

from catalog_bot.catalog import Product, build_reply, find_products


class BroadModelsTyposTests(unittest.TestCase):
    def setUp(self):
        def item(name, price, *, country='🇪🇺', stock=None):
            return Product(name, Decimal(price), stock, country=country)

        self.f8 = item('POCO F8 12/256 Black', '40000')
        self.f8_pro = item('POCO F8 Pro 12/256 Black', '47000')
        self.f8_ultra_black = item('POCO F8 Ultra 12/256 Black', '53200')
        self.f8_ultra_blue = item('POCO F8 Ultra 12/512 Blue', '59000')
        self.f9 = item('POCO F9 Pro 12/256 Green', '52000')
        self.redmi8 = item('Redmi 8 12/256 Black', '20000')
        self.iphone17 = item('iPhone 17 256 Black', '70000', country='🇨🇳')
        self.iphone17pro = item('iPhone 17 Pro 256 Black', '80000', country='🇨🇳')
        self.iphone17pm = item('iPhone 17 Pro Max 512 Blue', '90000', country='🇺🇸')
        self.iphone18 = item('iPhone 18 256 Black', '99000', country='🇨🇳')
        self.xiaomi17 = item('Xiaomi 17 12/256 Black', '50000')
        self.s26 = item('Galaxy S26 12/256 Black SM-S942B/DS', '60000', country='🇿🇦')
        self.s26fe = item('Galaxy S26 FE 8/256 Black SM-S741B/DS', '46800', country='🇷🇺')
        self.s26ultra = item('Galaxy S26 Ultra 12/512 Black SM-S948B/DS', '91500', country='🇨🇱')
        self.s25ultra = item('Galaxy S25 Ultra 12/512 Black SM-S938B/DS', '70600', country='🇨🇱')
        self.sold = item('POCO F8 Ultra 12/256 White', '54000', stock=0)
        self.products = [self.f8, self.f8_pro, self.f8_ultra_black, self.f8_ultra_blue,
                         self.f9, self.redmi8, self.iphone17, self.iphone17pro,
                         self.iphone17pm, self.iphone18, self.xiaomi17, self.s26,
                         self.s26fe, self.s26ultra, self.s25ultra, self.sold]

    def matches(self, query):
        return set(find_products(query, self.products)[1])

    def test_poco_f8_without_version_falls_back_to_named_versions_if_base_absent(self):
        self.products.remove(self.f8)
        expected = {self.f8_pro, self.f8_ultra_black, self.f8_ultra_blue}
        for text in ('поко ф8', 'poco f8', 'POCO F8', 'поко ф8 есть?'):
            with self.subTest(text=text):
                found = find_products(text, self.products)[1]
                self.assertEqual(set(found), expected)
                response = build_reply(found)
                for item in expected:
                    self.assertIn(item.name, response)
                self.assertNotIn('POCO F9', response)
                self.assertNotIn('Redmi', response)

    def test_base_version_takes_priority_when_available(self):
        self.assertEqual(self.matches('поко ф8'), {self.f8})
        self.assertEqual(self.matches('поко ф8 256 черный'), {self.f8})

    def test_explicit_version_memory_color_and_region_remain_constraints(self):
        cases = {
            'поко ф8 ультра': {self.f8_ultra_black, self.f8_ultra_blue},
            'поко ф8 про': {self.f8_pro},
            'поко ф8 256 черный': {self.f8},
            'поко ф8 512 синий': {self.f8_ultra_blue},
            'поко ф8 512 черный': set(),
            'поко ф8 256 белый': set(),
            'поко ф8 256 черный 🇺🇸': set(),
            'поко ф8 256 черный 🇪🇺': {self.f8},
            'поко ф9': {self.f9},
            'поко ф7': set(),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.matches(query), expected)

    def test_iphone_and_samsung_keep_explicit_base_and_version_distinct(self):
        cases = {
            'айфон 17': {self.iphone17},
            'iphone 17 256 black': {self.iphone17},
            'айфон 17 про 256 черный': {self.iphone17pro},
            'айфон 17 про макс 512 синий': {self.iphone17pm},
            'айфон 19': set(),
            'самсунг с26': {self.s26},
            'samsung s26 256 black': {self.s26},
            'самсунг с26 ультра 512 черный': {self.s26ultra},
            'самсунг с27': set(),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.matches(query), expected)

    def test_brand_spelling_typos_preserve_exact_models_and_brands(self):
        f8 = {self.f8}
        cases = {
            'pokoo f8': f8,
            'пко ф8': f8,
            'samsng s26': {self.s26},
            'самунг с26': {self.s26},
            'pokoo f7': set(),
            'пко ф7': set(),
            'samsng s27': set(),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.matches(query), expected)

    def test_version_and_color_typos_do_not_relax_constraints(self):
        cases = {
            'покко ф8 ултра чернй': {self.f8_ultra_black},
            'pocoo f8 ultrra blak': {self.f8_ultra_black},
            'айфонн 17 про 256 чернй': {self.iphone17pro},
            'iphon 18 256 black': {self.iphone18},
            'покко ф7 ултра чернй': set(),
            'pocoo f8 ultrra whte': set(),
            'покко ф8 ултра 128 чернй': set(),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.matches(query), expected)


if __name__ == '__main__':
    unittest.main()
