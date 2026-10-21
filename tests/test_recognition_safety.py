"""Independent adversarial catalog checks for strict product recognition.

Every catalog contains plausible alternatives and assertions use supplier IDs,
so recognizing a familiar word alone cannot make these tests pass.
"""

import unittest
from decimal import Decimal

from catalog_bot.catalog import Product, find_products


def offer(identifier, name, **kwargs):
    return Product(name=name, sku=identifier, **kwargs)


class RecognitionSafetyTests(unittest.TestCase):
    def assert_ids(self, query, products, expected):
        actual = [item.sku for item in find_products(query, products)[1]]
        self.assertEqual(actual, expected, query)

    def test_explicit_iphone_never_becomes_another_brand(self):
        products = [
            offer('APPLE17', 'Apple iPhone 17 256GB Black'),
            offer('XIAOMI17', 'Xiaomi 17 256GB Black'),
            offer('REDMI17', 'Xiaomi Redmi 17 256GB Black'),
            offer('POCO17', 'POCO 17 256GB Black'),
            offer('SAMSUNG17', 'Samsung Galaxy A17 256GB Black'),
        ]
        for query in ('Куплю iPhone 17 256 Black', 'Нужен айфон17 256 черный'):
            with self.subTest(query=query):
                self.assert_ids(query, products, ['APPLE17'])

    def test_conflicting_manufacturer_in_catalog_is_not_an_iphone(self):
        products = [
            offer('APPLE17', 'Apple iPhone 17 256GB'),
            offer('CONFLICTMI', 'Xiaomi iPhone 17 256GB'),
            offer('CONFLICTSAM', 'Samsung iPhone 17 256GB'),
            offer('TWO_MODELS', 'iPhone 17 Xiaomi 17 256GB'),
        ]
        self.assert_ids('Куплю iPhone 17 256', products, ['APPLE17'])

    def test_explicit_accessory_manufacturer_is_preserved(self):
        products = [
            offer('APPLECASE', 'Apple чехол для iPhone 17 Black'),
            offer('XIAOMICASE', 'Xiaomi чехол для iPhone 17 Black'),
            offer('BASEUSCASE', 'Baseus чехол для iPhone 17 Black'),
            offer('APPLEPHONE', 'Apple iPhone 17 Black'),
        ]
        self.assert_ids('Куплю Apple чехол для iPhone 17 Black', products, ['APPLECASE'])
        self.assert_ids('Куплю Xiaomi чехол для iPhone 17 Black', products, ['XIAOMICASE'])
        # A compatibility target alone does not promise an Apple-made accessory.
        self.assert_ids('Куплю чехол для iPhone 17 Black', products,
                        ['APPLECASE', 'XIAOMICASE', 'BASEUSCASE'])

    def test_sku_does_not_erase_an_explicit_manufacturer(self):
        products = [offer('CASE17', 'Xiaomi чехол для iPhone 17 Black')]
        self.assert_ids('CASE17', products, ['CASE17'])
        self.assert_ids('Куплю Apple CASE17', products, [])
        self.assert_ids('Куплю Samsung CASE17', products, [])
        self.assert_ids('Куплю iPhone 17 CASE17', products, [])

    def test_sku_cannot_erase_family_memory_ram_or_version(self):
        products = [
            offer('MI17256', 'Xiaomi 17 Pro 12/256GB Black'),
            offer('OP15R512', 'OnePlus 15R 12/512GB Black'),
        ]
        for query in ('iPhone 17 MI17256', 'MI17256 512GB', 'MI17256 16 RAM',
                      'MI17256 Ultra', 'OnePlus 15 OP15R512'):
            with self.subTest(query=query):
                self.assert_ids(query, products, [])

    def test_supplier_identifiers_require_complete_exact_characters(self):
        products = [
            offer('A17X256', 'Apple iPhone 17 256GB Black'),
            offer('A17X258', 'Apple iPhone 17 256GB White'),
        ]
        self.assert_ids('A17X256', products, ['A17X256'])
        for query in ('A17X25', 'A17X257', 'A18X256', 'A17Y256'):
            with self.subTest(query=query):
                self.assert_ids(query, products, [])

    def test_two_brand_or_model_requests_do_not_merge_attributes(self):
        products = [
            offer('APPLE17', 'Apple iPhone 17 256GB Black'),
            offer('XIAOMI17', 'Xiaomi 17 256GB Black'),
            offer('APPLE18', 'Apple iPhone 18 256GB Black'),
        ]
        for query in ('Куплю iPhone 17 Xiaomi 17 256 Black',
                      'Куплю iPhone 17 Samsung 17 256 Black',
                      'Куплю iPhone 17 и iPhone 18 256 Black'):
            with self.subTest(query=query):
                self.assert_ids(query, products, [])

    def test_short_iphone_is_ambiguous_with_a_matching_other_brand(self):
        products = [
            offer('APPLE17', 'Apple iPhone 17 Pro Max 256GB Black'),
            offer('REDMI17', 'Redmi Note 17 Pro Max 256GB Black'),
            offer('APPLE18', 'Apple iPhone 18 Pro Max 256GB Black'),
        ]
        self.assert_ids('17 Pro Max 256 Black', products, [])
        self.assert_ids('iPhone 17 Pro Max 256 Black', products, ['APPLE17'])
        self.assert_ids('18 Pro Max 256 Black', products, ['APPLE18'])

    def test_sim_versions_are_explicit_and_not_inferred_from_country(self):
        products = [
            offer('ONE', 'iPhone 17 256GB Black 1 SIM', country='🇨🇳'),
            offer('TWO', 'iPhone 17 256GB Black 2 SIM', country='🇺🇸'),
            offer('ESIM', 'iPhone 17 256GB Black eSIM', country='🇨🇳'),
            offer('COMBO', 'iPhone 17 256GB Black SIM+eSIM', country='🇺🇸'),
            offer('UNKNOWN', 'iPhone 17 256GB Black', country='🇨🇳'),
        ]
        # A physical SIM request also accepts the explicit SIM+eSIM combination.
        # This follows the documented policy; it does not infer SIM from a region.
        for sim, expected in (('1 SIM', ['ONE', 'COMBO']), ('2 SIM', ['TWO']),
                              ('eSIM', ['ESIM']), ('SIM+eSIM', ['COMBO'])):
            with self.subTest(sim=sim):
                self.assert_ids('Куплю iPhone 17 256 Black ' + sim, products, expected)
        self.assert_ids('iPhone 17 256 Black SIM eSIM', products, [])

    def test_samsung_s_and_c_models_do_not_fuzzy_merge(self):
        products = [
            offer('S26', 'Samsung Galaxy S26 Ultra 512GB Black'),
            offer('S25', 'Samsung Galaxy S25 Ultra 512GB Black'),
            offer('A26', 'Samsung Galaxy A26 Ultra 512GB Black'),
        ]
        self.assert_ids('Samsung Galaxy S26 Ultra 512 Black', products, ['S26'])
        self.assert_ids('Самсунг С26 Ultra 512 черный', products, ['S26'])
        self.assert_ids('Samsung Galaxy C26 Ultra 512 Black', products, [])
        self.assert_ids('Samsung Galaxy S27 Ultra 512 Black', products, [])

    def test_oneplus_letter_suffix_is_part_of_model(self):
        products = [
            offer('OP15', 'OnePlus 15 12/512GB Black'),
            offer('OP15R', 'OnePlus 15R 12/512GB Black'),
            offer('OP15T', 'OnePlus 15T 12/512GB Black'),
            offer('OP16R', 'OnePlus 16R 12/512GB Black'),
        ]
        self.assert_ids('OnePlus 15R 12/512 Black', products, ['OP15R'])
        self.assert_ids('OnePlus 15 12/512 Black', products, ['OP15'])
        self.assert_ids('OnePlus 15S 12/512 Black', products, [])

    def test_model_variants_never_silently_relax(self):
        iphone = [
            offer('BASE', 'iPhone 17 256GB Black'),
            offer('PRO', 'iPhone 17 Pro 256GB Black'),
            offer('PM', 'iPhone 17 Pro Max 256GB Black'),
            offer('PLUS', 'iPhone 17 Plus 256GB Black'),
        ]
        for version, expected in (('', ['BASE']), ('Pro', ['PRO']),
                                  ('Pro Max', ['PM']), ('Plus', ['PLUS'])):
            with self.subTest(version=version):
                self.assert_ids(f'iPhone 17 {version} 256 Black', iphone, expected)
        samsung = [offer('S26', 'Samsung Galaxy S26 256GB Black'),
                   offer('S26FE', 'Samsung Galaxy S26 FE 256GB Black')]
        self.assert_ids('Samsung S26 FE 256 Black', samsung, ['S26FE'])
        self.assert_ids('Samsung S26 256 Black', samsung, ['S26'])

    def test_ram_storage_and_lite_are_independent_constraints(self):
        products = [
            offer('H600L8256', 'Honor 600 Lite 8/256GB Gold'),
            offer('H600L1256', 'Honor 600 Lite 12/256GB Gold'),
            offer('H600L8512', 'Honor 600 Lite 8/512GB Gold'),
            offer('H6008256', 'Honor 600 8/256GB Gold'),
            offer('H601L8256', 'Honor 601 Lite 8/256GB Gold'),
        ]
        self.assert_ids('Honor 600 Lite 8/256 Gold', products, ['H600L8256'])
        self.assert_ids('Honor 600 Lite 12/256 Gold', products, ['H600L1256'])
        self.assert_ids('Honor 600 Lite 8/512 Gold', products, ['H600L8512'])
        self.assert_ids('Honor 600 Lite 8/256 12 RAM Gold', products, [])

    def test_budget_and_offer_eligibility_remain_hard_constraints(self):
        products = [
            offer('CHEAP', 'iPhone 17 256GB Black', price=Decimal('60000')),
            offer('EXPENSIVE', 'iPhone 17 256GB Black', price=Decimal('65001')),
            offer('NO_PRICE', 'iPhone 17 256GB Black'),
            offer('SOLD_OUT', 'iPhone 17 256GB Black', price=Decimal('50000'), stock=0),
            offer('WHOLESALE', 'iPhone 17 256GB Black', price=Decimal('50000'), min_qty=5),
            offer('WARNING', 'iPhone 17 256GB Black', price=Decimal('50000'), warnings=('проверить цену',)),
            offer('OTHER_MEMORY', 'iPhone 17 512GB Black', price=Decimal('60000')),
        ]
        self.assert_ids('Куплю iPhone 17 256 Black до 65 000', products, ['CHEAP'])

    def test_colors_and_shades_are_not_nearest_color_choices(self):
        products = [
            offer('BLUE', 'iPhone 17 256GB Blue'),
            offer('NAVY', 'iPhone 17 256GB Navy'),
            offer('BLACK', 'iPhone 17 256GB Black'),
            offer('NATURAL', 'iPhone 17 256GB Natural Titanium'),
            offer('DESERT', 'iPhone 17 256GB Desert Titanium'),
        ]
        self.assert_ids('iPhone 17 256 темно-синий', products, ['NAVY'])
        self.assert_ids('iPhone 17 256 Natural Titanium', products, ['NATURAL'])
        self.assert_ids('iPhone 17 256 Desert Titanium', products, ['DESERT'])
        self.assert_ids('iPhone 17 256 Blue Black', products, [])
        self.assert_ids('iPhone 17 256 Blue или Black', products, ['BLUE', 'BLACK'])

    def test_unknown_qualifier_survives_normalizing_known_words(self):
        products = [offer('PM17', 'iPhone 17 Pro Max 256GB Black')]
        self.assert_ids('Куплю айфон17 про макс 256 черный', products, ['PM17'])
        for unknown in ('несуществующий', 'опытный', 'specialedition', 's26'):
            with self.subTest(unknown=unknown):
                self.assert_ids('Куплю айфон17 про макс 256 черный ' + unknown, products, [])

    def test_negation_sales_and_chatter_do_not_trigger_offer(self):
        products = [offer('APPLE17', 'iPhone 17 256GB Black')]
        for query in ('Куплю iPhone 17 256 не черный',
                      'Куплю iPhone 17 256 кроме черного',
                      'Куплю iPhone 17 256 без eSIM',
                      'Продам iPhone 17 256 Black',
                      'Куплю и продам iPhone 17 256 Black',
                      'У меня iPhone 17 256 Black',
                      'Обзор iPhone 17 256 Black'):
            with self.subTest(query=query):
                self.assert_ids(query, products, [])


if __name__ == '__main__':
    unittest.main()
