import unittest
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import CatalogError, Product, build_reply, find_products, parse_catalog

SAMPLE = Path(__file__).resolve().parent.parent / 'examples' / 'catalog.txt'


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.products = parse_catalog(SAMPLE.read_text())

    def names(self, text, bare=True):
        return [p.name for p in find_products(text, self.products, bare)[1]]

    def test_broad_base_model_returns_available_configurations(self):
        found = self.names('Нужен iPhone 17')
        self.assertEqual(len(found), 2)
        self.assertTrue(all('Pro' not in x for x in found))

    def test_exact_configuration_from_screenshot(self):
        found = self.names('Предложите\n\niPhone 17 Pro Max 256 silver sim')
        self.assertEqual(found, ['iPhone 17 Pro Max 256GB Silver SIM+eSIM'])

    def test_short_iphone_without_brand(self):
        self.assertEqual(self.names('18 pro max 1tb glacier esim'), ['iPhone 18 Pro Max 1TB Glacier eSIM'])

    def test_cyrillic_joined_model(self):
        self.assertEqual(len(self.names('Куплю айфон17 256 черный сим')), 1)

    def test_model_and_variants_do_not_cross(self):
        for text in ['iPhone 16', 'iPhone 17 Pro Max 256 Black SIM', 'iPhone 17 Pro 512 Blue eSIM', '18 pro max 2tb silver sim+esim']:
            with self.subTest(text=text):
                self.assertEqual(self.names(text), [])
        self.assertEqual(self.names('iPhone 17 Pro 256 Silver SIM'), ['iPhone 17 Pro 256GB Silver SIM'])

    def test_no_guessing_missing_or_conflicting_characteristics(self):
        for text in ['iPhone 17 256 White SIM', 'iPhone 17 256 Black esim', 'iPhone 17 256 Black SIM new']:
            with self.subTest(text=text):
                self.assertEqual(self.names(text), [])

    def test_buy_sell_and_conversation(self):
        for text in ['Продам iPhone 17', 'В наличии iPhone 17', 'iPhone 17 85000', 'у меня iPhone 17', 'iPhone 17 хорошая камера?', 'не нужен iPhone 17', 'ищу iPhone 17 не black']:
            with self.subTest(text=text):
                self.assertEqual(self.names(text), [])

    def test_bare_mode_can_be_disabled(self):
        self.assertEqual(self.names('iPhone 17', bare=False), [])
        self.assertEqual(len(self.names('Куплю iPhone 17', bare=False)), 2)

    def test_sku_must_exist_and_respect_color(self):
        self.assertEqual(len(self.names('MDE44')), 1)
        self.assertEqual(self.names('MDE45'), [])
        self.assertEqual(self.names('MDE44 Black'), [])
        self.assertEqual(self.names('MDE44 iPhone 18 Pro Max'), [])
        self.assertEqual(self.names('MDE44 iPhone 17'), [])
        self.assertEqual(self.names('MDE44 plus'), [])
        self.assertEqual(self.names('MDE44 A3094'), [])

    def test_samsung_capacity_and_ram(self):
        self.assertEqual(len(self.names('A57 12/512 A.Lilac ???')), 1)
        self.assertEqual(self.names('A57 8/512 A.Lilac'), [])

    def test_nothing_phone_from_screenshot(self):
        self.assertEqual(len(self.names('Nothing Phone3 512GB 16GB A024 White')), 1)
        self.assertEqual(self.names('Nothing Phone3 512GB 8GB A024 White'), [])

    def test_generic_product_and_multiline_glasses(self):
        self.assertEqual(len(self.names('Колонка Marshall EMBERTON III Black')), 1)
        self.assertEqual(self.names('Marshall Emberton II Black'), [])
        self.assertEqual(len(self.names('Куплю\n\nОчки RayBan Gen 2 фотохром черный или зеленый\nРазмер M')), 1)
        self.assertEqual(self.names('Очки RayBan Gen 1 фотохром черный Размер M'), [])

    def test_out_of_stock(self):
        self.assertEqual(self.names('Предложите\niPad 11 128GB Wi-Fi Yellow'), [])

    def test_budget_is_not_price_invented_by_bot(self):
        self.assertEqual(len(self.names('Куплю iPhone 17 бюджет 90 000')), 1)
        self.assertEqual(self.names('Куплю iPhone 17 до 80 000'), [])

    def test_separate_lines_do_not_mix_attributes(self):
        text = 'Куплю\niPhone 17 256 White SIM\niPhone 17 512 Black eSIM'
        self.assertEqual(self.names(text), [])

    def test_reply_uses_only_catalog_values(self):
        found = find_products('iPhone 17 256 Black SIM', self.products)[1]
        reply = build_reply(found)
        self.assertIn('85 000 ₽', reply)
        self.assertNotIn('95 000', reply)
        self.assertEqual(build_reply([Product('Marshall Emberton III Black')]), 'Есть в наличии:\n• Marshall Emberton III Black')

    def test_supplier_reply_starts_with_product_and_has_no_automatic_header(self):
        product = Product('Galaxy S25 12/256 Navy', Decimal('49000'), None)
        expected = '• Galaxy S25 12/256 Navy — 49 000 ₽'
        for intro in (None, '', 'Есть в наличии:', 'По актуальному прайсу:'):
            with self.subTest(intro=intro):
                self.assertEqual(build_reply([product], intro), expected)

    def test_custom_intro_and_contact_are_preserved_without_automatic_supplier_text(self):
        product = Product('Galaxy S25 12/256 Navy', Decimal('49000'), None)
        self.assertEqual(build_reply([product], 'Предложение:', '@seller'),
                         'Предложение:\n• Galaxy S25 12/256 Navy — 49 000 ₽\n\n@seller')


class ImportTests(unittest.TestCase):
    def test_headers_csv_and_decimal(self):
        products = parse_catalog('Название;Цена;Остаток;Артикул\nMarshall Emberton III Black;12 500,50;2;TEST-1')
        self.assertEqual(products[0].price, Decimal('12500.50'))
        self.assertEqual(products[0].stock, 2)

    def test_inline_price_and_names_only(self):
        products = parse_catalog('iPhone 17 256 Black SIM — 85 000\niPhone 17 Pro 256 Silver SIM')
        self.assertEqual(products[0].price, Decimal('85000'))
        self.assertIsNone(products[1].price)

    def test_bad_rows_rejected_atomically(self):
        for text in ['', '# только комментарий', 'iPhone', 'iPhone 17|nan|1', 'iPhone 17|100|-1', 'iPhone 17|100|1|CODE\niPhone 18|100|1|CODE', 'iPhone 17\niPhone 17', 'Название|Неизвестно\niPhone 17|1']:
            with self.subTest(text=text):
                with self.assertRaises(CatalogError):
                    parse_catalog(text)

    def test_quoted_csv(self):
        products = parse_catalog('name,price,stock,sku\n"Marshall Emberton III Black",12000,1,ABC')
        self.assertEqual(products[0].sku, 'ABC')


if __name__ == '__main__':
    unittest.main()
