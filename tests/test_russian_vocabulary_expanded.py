"""Natural Russian customer messages against prices from the provided catalog."""
from decimal import Decimal
import unittest

from catalog_bot.catalog import Product, find_products


class RussianVocabularyExpandedTests(unittest.TestCase):
    def setUp(self):
        self.offers = [
            'iPhone 16 Pro Max 512 Black',
            'Galaxy S26 12/256 Black SM-S942B/DS',
            'Galaxy S26 Ultra 12/256 Black SM-S948B/DS',
            'Galaxy S26 Ultra 12/512 Black SM-S948B/DS',
            'POCO F9 Pro 12/256 Green',
            'POCO F8 Ultra 12/256 Black',
            'POCO X6 5G 12/256 White',
            'Xiaomi 17T 12/512 Black',
            'Redmi 17 6/128 Deep Blue NFC',
            'OnePlus 15 5G 16/512 Ultra Violet',
            'OnePlus 15 12/256 Infinite Black CPH2749',
            'Honor Magic 8 Pro 16/1TB Sunrise Gold',
            'Google Pixel 11 Pro 256 Obsidian',
            'Google Pixel 11 Pro XL 256 Obsidian',
            'Sony WH-1000XM6 Black',
            'Sony WH-1000XM6 Silver',
            'Nintendo Switch Lite Gray',
            'Nintendo Switch Lite Coral',
            'GoPro Max 2 8K 360',
            'DJI Osmo Pocket 4 Bundle Standart Combo',
            'Insta 360 X5 - 8K 360 Action Cam Satin White',
            'Insta 360 X6 - Essentials Bundle Midnight Black',
            'Fujifilm instax Mini Evo Cinema',
            'Meta Ray-Ban Skyler Gen2 S52 (F Shiny Black, L G15 Green) RW4014',
            'Яндекс Стрит Зеленая Алиса',
            'Oura Ring 5 - Size 9 Black',
            'Galaxy Buds 3 Pro Silver SM-R630',
            'Apple USB-C 20W Power Adapter A2347',
            'Чехол iPhone 16 Pro Max 512 Black',
        ]
        self.products = [Product(name, Decimal('10000') + i, None)
                         for i, name in enumerate(self.offers)]

    def names(self, request):
        return {p.name for p in find_products(request, self.products)[1]}

    def test_natural_purchase_requests_and_units(self):
        cases = {
            'ищу айфон 16 про макс 512 гигабайт черный': {'iPhone 16 Pro Max 512 Black'},
            'нужен самсунг с26 ультра 512 гигов черный': {'Galaxy S26 Ultra 12/512 Black SM-S948B/DS'},
            'куплю поко ф9 про 256 зеленый': {'POCO F9 Pro 12/256 Green'},
            'нужен ванплюс 15 512 фиолетовый': {'OnePlus 15 5G 16/512 Ultra Violet'},
            'ищу хонор мэджик 8 про 1 терабайт золотой': {'Honor Magic 8 Pro 16/1TB Sunrise Gold'},
            'нужен пиксель 11 про икс эль 256 обсидиан': {'Google Pixel 11 Pro XL 256 Obsidian'},
            'куплю сони хм6 серебристые': {'Sony WH-1000XM6 Silver'},
            'ищу нинтендо свитч лайт серый': {'Nintendo Switch Lite Gray'},
        }
        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(self.names(request), expected)

    def test_russian_names_for_families_and_hardware(self):
        cases = {
            'поко икс 6 белый': {'POCO X6 5G 12/256 White'},
            'редми 17 синий': {'Redmi 17 6/128 Deep Blue NFC'},
            'сяоми 17т блэк': {'Xiaomi 17T 12/512 Black'},
            'гоупро макс 2': {'GoPro Max 2 8K 360'},
            'диджиай осмо покет 4': {'DJI Osmo Pocket 4 Bundle Standart Combo'},
            'инста 360 икс 5 белая': {'Insta 360 X5 - 8K 360 Action Cam Satin White'},
            'фуджифильм инстакс мини эво': {'Fujifilm instax Mini Evo Cinema'},
            'мета рейбан скайлер ген2': {'Meta Ray-Ban Skyler Gen2 S52 (F Shiny Black, L G15 Green) RW4014'},
            'яндекс стрит зеленая': {'Яндекс Стрит Зеленая Алиса'},
            'аура ринг 5 размер 9 черный': {'Oura Ring 5 - Size 9 Black'},
            'самсунг бадс 3 про серебро': {'Galaxy Buds 3 Pro Silver SM-R630'},
        }
        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(self.names(request), expected)

    def test_short_conversational_purchase_phrases(self):
        iphone = {'iPhone 16 Pro Max 512 Black'}
        poco = {'POCO F9 Pro 12/256 Green'}
        cases = {
            'мне нужен айфон 16 про макс на 512 черный': iphone,
            'ищу айфон 16 про макс 512 черный срочно': iphone,
            'у кого есть поко ф9 про зеленый': poco,
            'есть ли поко ф9 про зеленый?': poco,
            'хочу купить поко ф9 про зеленый': poco,
            'привет, ищу поко ф9 про зеленый': poco,
            'добрый день, куплю поко ф9 про на 256 гигов 2шт': poco,
            'сколько стоит самсунг с26 ультра 512 черный': {'Galaxy S26 Ultra 12/512 Black SM-S948B/DS'},
            'поко ф9 про 256 зеленый в наличии?': poco,
            'есть ли в наличии поко ф9 про 256 зеленый': poco,
        }
        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(self.names(request), expected)

    def test_wrong_generation_color_memory_and_brands_are_not_guessed(self):
        for request in ('ищу айфон 17 про макс 512 черный',
                        'поко ф9 про черный',
                        'самсунг с26 ультра 1024 черный',
                        'пиксель 11 про икс эль 512 обсидиан',
                        'сони хм6 белые',
                        'инста 360 икс 7 белая',
                        'самсунг айфон 16 про макс 512',
                        'поко сяоми айфон 16 про макс 512'):
            with self.subTest(request=request):
                self.assertEqual(self.names(request), set())

    def test_selling_or_negated_request_does_not_generate_offer(self):
        for request in ('продам поко ф9 про 256 зеленый',
                        'продам поко ф9 про 256 зеленый в наличии?',
                        'поко ф9 про 256 зеленый в наличии',
                        'поко ф9 про 256 зеленый цена 52000 в наличии?',
                        'не нужен айфон 16 про макс 512 черный',
                        'мне айфон 16 про макс 512 черный не нужен',
                        'ищу поко ф9 про не зеленый'):
            with self.subTest(request=request):
                self.assertEqual(self.names(request), set())
