import unittest
from decimal import Decimal

from catalog_bot.catalog import Product, find_products, inspect_request


class StrictMatchingTests(unittest.TestCase):
    def names(self, query, *names):
        return [p.name for p in find_products(query, [Product(n) for n in names])[1]]

    def test_device_is_not_accessory(self):
        self.assertEqual(self.names('Куплю iPhone 17', 'iPhone 17', 'Чехол iPhone 17',
                                    'iPhone 17 Case', 'Стекло iPhone 17', 'Экран iPhone 17'), ['iPhone 17'])

    def test_condition_must_not_be_silently_relaxed(self):
        self.assertEqual(self.names('Куплю iPhone 17', 'iPhone 17', 'iPhone 17 used',
                                    'iPhone 17 refurbished', 'iPhone 17 (Мятая коробка)',
                                    'iPhone 17 Demo'), ['iPhone 17'])
        self.assertEqual(self.names('Куплю iPhone 17 used', 'iPhone 17', 'iPhone 17 used'), ['iPhone 17 used'])

    def test_nfc_is_three_valued(self):
        items = ('Note15 8/256 Blue NFC', 'Note15 8/256 Blue (NO NFC)', 'Note15 8/256 Blue')
        self.assertEqual(self.names('Куплю Note 15 NFC', *items), [items[0]])
        self.assertEqual(self.names('Куплю Note 15 без NFC', *items), [items[1]])

    def test_cellular_and_wifi_are_not_interchangeable(self):
        items = ('iPad 11 128 Wi-Fi', 'iPad 11 128 Cellular', 'iPad 11 128 Wi-Fi+LTE', 'iPad 11 128')
        self.assertEqual(self.names('Нужен iPad 11 Cellular', *items), list(items[1:3]))
        self.assertEqual(self.names('Нужен iPad 11 WiFi', *items), [items[0]])

    def test_brand_only_lists_products_but_a_model_stays_specific(self):
        self.assertEqual(self.names('Куплю Sony', 'Sony WH1000XM6', 'Sony WH1000XM5'), ['Sony WH1000XM6', 'Sony WH1000XM5'])
        self.assertEqual(self.names('Куплю Sony WH1000XM6', 'Sony WH1000XM6', 'Sony WH1000XM5'), ['Sony WH1000XM6'])

    def test_letter_only_model_requires_full_catalog_model(self):
        self.assertEqual(self.names('Куплю Nintendo Switch Lite', 'Nintendo Switch Lite Gray'), ['Nintendo Switch Lite Gray'])
        self.assertEqual(self.names('Куплю Nintendo', 'Nintendo Switch Lite Gray'), ['Nintendo Switch Lite Gray'])
        self.assertEqual(self.names('Куплю Marshall Emberton', 'Marshall Emberton III'), [])

    def test_conflicting_storage_is_not_implicit_or(self):
        items = ('iPhone 17 256 Black', 'iPhone 17 512 Green')
        self.assertEqual(self.names('iPhone 17 256 512', *items), [])
        self.assertEqual(self.names('iPhone 17 256 512 Black или Green', *items), [])
        self.assertEqual(self.names('iPhone 17 256 или 512', *items), list(items))

    def test_or_is_scoped_to_the_attribute(self):
        self.assertEqual(self.names('iPhone 17 256 или 512 Black Green', 'iPhone 17 256 Black'), [])

    def test_ram_32_is_not_storage(self):
        self.assertEqual(self.names('Honor Magic8 Pro 32/256', 'Honor Magic 8 Pro 32/256', 'Honor Magic 8 Pro 16/256'), ['Honor Magic 8 Pro 32/256'])
        self.assertEqual(self.names('Honor Magic8 Pro 8/256 16 RAM', 'Honor Magic 8 Pro 16/256'), [])

    def test_sku_cannot_override_attributes_or_condition(self):
        items = [Product('Note15 256 NO NFC', sku='CODE1'), Product('iPhone 17 used', sku='CODE2')]
        self.assertEqual(find_products('CODE1 NFC', items)[1], [])
        self.assertEqual(find_products('CODE2', items)[1], [])

    def test_sim_plus_esim_requires_both_explicitly(self):
        items = ('iPhone 17 SIM', 'iPhone 17 SIM+eSIM', 'iPhone 17 eSIM')
        self.assertEqual(self.names('iPhone 17 SIM+eSIM', *items), [items[1]])
        self.assertEqual(self.names('iPhone 17 SIM eSIM', *items), [])

    def test_watch_case_is_not_phone_cover(self):
        name = 'Google Pixel Watch 4 41mm Silver Aluminum Case Wi-Fi'
        self.assertEqual(self.names('Куплю Pixel Watch 4 41mm', name, 'Case for Pixel Watch 4 41mm'), [name])

    def test_budget_numbers_are_not_capacities(self):
        products = [Product('iPhone 17 256', Decimal('60000')), Product('iPhone 17 512', Decimal('62000'))]
        self.assertEqual(len(find_products('Куплю iPhone 17 до 128 000', products)[1]), 2)
        self.assertEqual(find_products('Куплю iPhone 17 256 до 64 000', products)[1], products[:1])

    def test_two_models_on_one_line_are_ambiguous(self):
        self.assertEqual(self.names('Куплю iPhone 17 и iPhone 18', 'iPhone 17', 'iPhone 18'), [])

    def test_frame_and_lens_colors_do_not_swap(self):
        items = ('Meta RayBan Gen2 F Black L Green', 'Meta RayBan Gen2 F Green L Black')
        self.assertEqual(self.names('Meta RayBan Gen2 F Black L Green', *items), [items[0]])

    def test_color_longest_alias_wins(self):
        self.assertEqual(self.names('iPhone 17 темно-синий', 'iPhone 17 Blue', 'iPhone 17 Navy'), ['iPhone 17 Navy'])

    def test_diagnostics_explain_filters_and_ambiguity(self):
        report = inspect_request('Куплю iPad 11 Cellular', [Product('iPad 11 Wi-Fi')])
        self.assertIn('связь: cellular', '\n'.join(report.details))
        self.assertIn('Wi-Fi / сотовая связь', '\n'.join(report.details))
        report = inspect_request('iPhone 17 256 512', [Product('iPhone 17 256')])
        self.assertIn('несколько значений памяти', '\n'.join(report.details))

    def test_implicit_iphone_requires_no_competing_family(self):
        items = ('iPhone 17 Pro Max 256', 'Note17 Pro Max 256')
        self.assertEqual(self.names('17 pro max 256', *items), [])
        self.assertEqual(self.names('iPhone 17 pro max 256', *items), [items[0]])
        self.assertEqual(self.names('17 pro max 256', items[0], 'Note17 Pro Max 512'), [items[0]])
        self.assertEqual(self.names('17 256', 'iPhone 17 256', 'Redmi 17 256'), [])

    def test_exact_sku_can_identify_accessory_without_category(self):
        product = Product('Чехол iPhone 17 Black', sku='CASE17')
        self.assertEqual(find_products('CASE17', [product])[1], [product])
        self.assertEqual(find_products('CASE17 White', [product])[1], [])
        self.assertEqual(find_products('CASE17 iPhone 17', [product])[1], [])

    def test_supplier_code_in_name_uses_same_strict_filters(self):
        product = Product('Meta Ray-Ban Wayfarer Gen2 S50 Black RW4012')
        self.assertEqual(find_products('RW4012', [product])[1], [product])
        self.assertEqual(find_products('RW4012 White', [product])[1], [])
        self.assertEqual(find_products('RW4012 iPhone 17', [product])[1], [])
        self.assertEqual(find_products('RW401', [product])[1], [])

    def test_capacity_is_never_a_supplier_identifier(self):
        self.assertEqual(self.names('256GB', 'iPhone 17 256GB'), [])
        self.assertEqual(find_products('телефон CASE17', [Product('Чехол iPhone 17', sku='CASE17')])[1], [])
