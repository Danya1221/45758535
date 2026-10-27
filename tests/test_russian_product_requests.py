"""Russian requests against complete, distinct offers from the supplier price."""
import unittest
from decimal import Decimal

from catalog_bot.catalog import Product, find_products


class RussianProductRequestsTests(unittest.TestCase):
    def setUp(self):
        self.names_in_price = [
            'POCO F9 Pro 12/256 Green',
            'POCO F8 Ultra 12/256 Black',
            'POCO X6 5G 12/256 Black',
            'POCO C95 Pro 8/256 Green',
            'Xiaomi 17T 12/512 Black',
            'Xiaomi 17T Pro 12/256 Deep Blue',
            'Xiaomi Pad 7 8/256 Gray',
            'Google Pixel 10a 5G 128 Obsidian',
            'Google Pixel 11 256 Obsidian',
            'Vivo X300 16/256 Purple',
            'Vivo X300S 12/256 Black',
            'Sony WH-1000XM6 Black',
            'Sony WH-1000XM6 Silver',
            'Sony WH-1000XM5 Black',
            'Insta 360 X5 - 8K 360 Action Cam Satin White',
            'Insta 360 X6 - Essentials Bundle Midnight Black',
            'Fujifilm instax Mini Evo Cinema',
            'Instax mini 13 Dreamy Blue',
            'Meta Ray-Ban Wayfarer Gen2 S50 (F Matte Black, L Transitions grey) RW4012',
            'Meta Ray-Ban Skyler Gen2 S52 (F Shiny Black, L G15 Green) RW4014',
            'iPhone 16 Pro Max 512 Black',
            'Galaxy S26 Ultra 12/512 Black SM-S948B/DS',
        ]
        self.products = [Product(name, Decimal('10000') + i, None)
                         for i, name in enumerate(self.names_in_price)]

    def names(self, text):
        return {item.name for item in find_products(text, self.products)[1]}

    def test_single_brand_cyrillic_is_equal_to_latin_without_cross_brand(self):
        expected = {name for name in self.names_in_price if name.startswith('POCO ')}
        for query in ('поко', 'ПОКО', 'Poco', 'poco'):
            with self.subTest(query=query):
                self.assertEqual(self.names(query), expected)

    def test_poco_model_letters_and_colors_remain_specific(self):
        cases = {
            'поко ф9 про зеленый': {'POCO F9 Pro 12/256 Green'},
            'поко ф8 ультра черный': {'POCO F8 Ultra 12/256 Black'},
            'поко х6 256 черный': {'POCO X6 5G 12/256 Black'},
            'поко икс 6': {'POCO X6 5G 12/256 Black'},
            'поко с95 про зеленый': {'POCO C95 Pro 8/256 Green'},
            'поко ф9 про черный': set(),
            'поко х7': set(),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.names(query), expected)

    def test_cyrillic_letters_in_other_product_families(self):
        cases = {
            'инста 360 х5': {'Insta 360 X5 - 8K 360 Action Cam Satin White'},
            'инста 360 икс 6': {'Insta 360 X6 - Essentials Bundle Midnight Black'},
            'инста 360 х7': set(),
            'сяоми 17т 512 черный': {'Xiaomi 17T 12/512 Black'},
            'сяоми пад 7 256 серый': {'Xiaomi Pad 7 8/256 Gray'},
            'пиксель 10а 128': {'Google Pixel 10a 5G 128 Obsidian'},
            'пиксель 11 256': {'Google Pixel 11 256 Obsidian'},
            'виво х300 фиолетовый': {'Vivo X300 16/256 Purple'},
            'виво х300 черный': set(),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.names(query), expected)

    def test_sony_short_code_and_colors_do_not_select_another_generation(self):
        cases = {
            'сони хм6': {'Sony WH-1000XM6 Black', 'Sony WH-1000XM6 Silver'},
            'сони хм6 черный': {'Sony WH-1000XM6 Black'},
            'сони хм6 серебро': {'Sony WH-1000XM6 Silver'},
            'сони хм5 черный': {'Sony WH-1000XM5 Black'},
            'сони хм6 белый': set(),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.names(query), expected)

    def test_natural_glasses_and_camera_names(self):
        cases = {
            'инстакс мини эво': {'Fujifilm instax Mini Evo Cinema'},
            'инстакс мини 13': {'Instax mini 13 Dreamy Blue'},
            'мета рейбан вейфарер ген2': {
                'Meta Ray-Ban Wayfarer Gen2 S50 (F Matte Black, L Transitions grey) RW4012'},
            'мета рейбан скайлер ген2': {
                'Meta Ray-Ban Skyler Gen2 S52 (F Shiny Black, L G15 Green) RW4014'},
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.names(query), expected)

    def test_conflicting_brands_or_absent_models_are_not_guessed(self):
        for query in ('поко айфон 16 про макс 512', 'поко ф99 про',
                      'айфон 17 про макс 512', 'виво х301', 'сяоми 18т'):
            with self.subTest(query=query):
                self.assertEqual(self.names(query), set())


if __name__ == '__main__':
    unittest.main()
