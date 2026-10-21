import json
import unittest
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import Product
from catalog_bot.prices import parse_price_feed
from catalog_bot.pricing import (CATEGORIES, MarkupPolicy, MarkupRule, category_for,
                                 format_policy, price_products, resolve_category)


class PricingRuleTests(unittest.TestCase):
    def test_decimal_input_aliases_and_boundaries(self):
        self.assertEqual(MarkupRule.parse('руб', '1 500,25'), MarkupRule('fixed', Decimal('1500.25')))
        self.assertEqual(MarkupRule.parse('%', '7.5'), MarkupRule('percent', Decimal('7.5')))
        self.assertEqual(MarkupRule.parse('fixed', '0').value, Decimal('0'))
        self.assertEqual(MarkupRule.parse('fixed', '100000000').value, Decimal('100000000'))
        self.assertEqual(MarkupRule.parse('percent', '1000').value, Decimal('1000'))

    def test_invalid_or_nonfinite_markup_is_rejected(self):
        for mode, value in (('fixed', '-1'), ('percent', '-0.1'), ('percent', '1000.01'),
                            ('fixed', '100000000.01'), ('fixed', 'NaN'), ('fixed', 'Infinity'),
                            ('fixed', '1e3'), ('fixed', '1 50'), ('fixed', '1,2.3'),
                            ('fixed', ''), ('unknown', '1'), ('fixed', 1.5), ('fixed', True)):
            with self.subTest(mode=mode, value=value), self.assertRaises(ValueError):
                MarkupRule.parse(mode, value)
        for value in (1.0, 1, True, Decimal('NaN'), Decimal('Infinity'), Decimal('-1')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MarkupRule('fixed', value)

    def test_policy_roundtrip_preserves_decimal_without_json_float(self):
        policy = (MarkupPolicy().with_rule('default', MarkupRule.parse('fixed', '500'))
                  .with_rule('телефоны', MarkupRule.parse('percent', '7,25')))
        data = json.loads(json.dumps(policy.to_data()))
        self.assertEqual(data['overrides']['phones']['value'], '7.25')
        self.assertEqual(MarkupPolicy.from_data(data), policy)
        self.assertEqual(MarkupPolicy.from_data().default.value, Decimal('0'))

    def test_policy_json_fails_closed(self):
        invalid = [[], 0, False, {'mystery': 1}, {'default': None},
                   {'default': {'mode': 'fixed', 'value': 1.1}},
                   {'default': {'mode': 'fixed', 'value': '10', 'extra': True}},
                   {'default': {'mode': 'percent', 'value': '-1'}},
                   {'overrides': []}, {'overrides': {'unknown': {'mode': 'fixed', 'value': '1'}}},
                   {'overrides': {'default': {'mode': 'fixed', 'value': '1'}}},
                   {'overrides': {'phones': {'mode': 'fixed', 'value': '1'},
                                  'телефоны': {'mode': 'fixed', 'value': '2'}}}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                MarkupPolicy.from_data(data)

    def test_rules_create_new_policies_and_category_overrides_replace_default(self):
        zero = MarkupPolicy()
        common = zero.with_rule('default', MarkupRule.parse('fixed', '500'))
        custom = common.with_rule('phones', MarkupRule.parse('percent', '10'))
        products = [Product('iPhone 17', Decimal('1000')), Product('Unknown Product', Decimal('1000'))]
        self.assertEqual([p.price for p in price_products(products, custom)], [Decimal('1100'), Decimal('1500')])
        self.assertEqual(zero.default.value, Decimal('0'))
        self.assertEqual(common.overrides, {})
        self.assertEqual(custom.without_rule('phones'), common)
        self.assertEqual(custom.without_rule('default').default.value, Decimal('0'))
        self.assertIn('phones', custom.without_rule('default').overrides)

    def test_explicit_zero_category_rule_exempts_common_markup(self):
        policy = MarkupPolicy(MarkupRule.parse('fixed', '500')).with_rule('phones', MarkupRule.parse('fixed', '0'))
        self.assertEqual(price_products([Product('iPhone 17', Decimal('1000'))], policy)[0].price, Decimal('1000'))

    def test_policy_copies_external_override_dictionary(self):
        external = {'phones': MarkupRule.parse('percent', '5')}
        policy = MarkupPolicy(overrides=external)
        external.clear()
        self.assertIn('phones', policy.overrides)

    def test_category_aliases_and_format_are_clear(self):
        self.assertEqual(resolve_category('По умолчанию'), 'default')
        self.assertEqual(resolve_category('Телефоны'), 'phones')
        self.assertEqual(resolve_category('микрофоны'), 'audio')
        for category in CATEGORIES:
            self.assertEqual(resolve_category(category), category)
        with self.assertRaises(ValueError):
            resolve_category('неизвестные товары')
        text = format_policy(MarkupPolicy(MarkupRule.parse('fixed', '1000')).with_rule('phones', MarkupRule.parse('percent', '7.5')))
        self.assertIn('Общее правило: +1 000 ₽', text)
        self.assertIn('Телефоны: +7,5%', text)
        self.assertIn('вместо общего правила', text)


class PricingCalculationTests(unittest.TestCase):
    def test_fixed_and_percent_round_half_up_to_kopeck_once(self):
        product = Product('iPhone 17', Decimal('0.05'))
        policy = MarkupPolicy(MarkupRule.parse('percent', '10'))
        self.assertEqual(price_products([product], policy)[0].price, Decimal('0.06'))
        self.assertEqual(price_products([Product('iPhone 17', Decimal('10'))],
                                        MarkupPolicy(MarkupRule.parse('fixed', '0.005')))[0].price, Decimal('10.01'))

    def test_zero_default_and_none_price_do_not_create_a_price(self):
        product = Product('iPhone 17', None)
        self.assertIsNone(price_products([product], MarkupPolicy(MarkupRule.parse('fixed', '500')))[0].price)
        self.assertEqual(price_products([Product('iPhone 17', Decimal('91500'))], MarkupPolicy())[0].price, Decimal('91500'))

    def test_source_and_all_non_price_fields_are_preserved(self):
        original = Product('Galaxy S26 Ultra 12/512 Black', Decimal('91500'), None, 'PHONE',
                           '🇿🇦', '🇿🇦', 5, ('проверить',))
        policy = MarkupPolicy(MarkupRule.parse('percent', '10'))
        priced = price_products([original], policy)[0]
        self.assertEqual(priced.price, Decimal('100650'))
        self.assertEqual(original.price, Decimal('91500'))
        for attribute in ('name', 'stock', 'sku', 'country', 'price_country', 'min_qty', 'warnings'):
            self.assertEqual(getattr(priced, attribute), getattr(original, attribute))
        self.assertEqual(price_products([original], policy)[0].price, priced.price)

    def test_maximum_rules_and_invalid_source_prices(self):
        product = Product('iPhone 17', Decimal('100000000'))
        self.assertEqual(price_products([product], MarkupPolicy(MarkupRule.parse('percent', '1000')))[0].price,
                         Decimal('1100000000.00'))
        self.assertEqual(price_products([product], MarkupPolicy(MarkupRule.parse('fixed', '100000000')))[0].price,
                         Decimal('200000000.00'))
        for value in (Decimal('-1'), Decimal('NaN'), Decimal('Infinity'), 1000.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                price_products([Product('iPhone 17', value)], MarkupPolicy())


class PricingCategoryTests(unittest.TestCase):
    def test_known_product_types_are_not_guessed_from_brand_alone(self):
        cases = {
            'phones': ['iPhone 16 Pro Max 512 Black', 'Galaxy S26 Ultra 12/512 Black',
                       'Galaxy Z Fold 8 12/512 Cream', 'Google Pixel 11 Pro XL 512 Obsidian',
                       'OnePlus 15 12/256 Infinite Black', 'Honor 600 Pro 12/512 Black',
                       'Honor Magic 8 Pro 16/1TB Gold', 'Redmi 17 6/128 Blue', 'POCO F8 Ultra 12/256 Black',
                       'Note 15 8/256 Black NFC', 'Realme GT 7T 12/256', 'Vivo X300 16/512',
                       'NOTHING Phone 4b 8/128 Black'],
            'tablets': ['Galaxy Tab A11 4/64 Gray', 'Xiaomi Pad 7 8/256 Gray',
                        'Redmi Pad Pro 5G 6/128 Mint', 'POCO Pad X1 8/512 Grey', 'iPad Pro M4 256'],
            'laptops': ['Galaxy Book 6 NP760VJG', 'Apple MacBook Air M4 16/256'],
            'wearables': ['Google Pixel Watch 5 WI-FI 41mm Aluminum Case Obsidian Active Band',
                          'Galaxy Watch9 44mm Silver SM-L350', 'Galaxy Ring SM-Q505 Titanium Gold',
                          'Google Fitbit Air Berry Pink', 'Oura Ring 5 Size 8 Black',
                          'Coros Pace 4 White Nylon', 'GARMIN FENIX 9 Pro 47mm'],
            'headphones': ['Galaxy Buds 4 Black SM-R540', 'Sony WH-1000XM6 Black',
                           'Sennheiser HD620S Black', 'Realme Buds T200 Lite Storm Grey', 'AirPods Pro 3'],
            'glasses': ['Meta Ray-Ban Wayfarer Gen2 S50 Black RW4012',
                        'Meta Ray-Ban Scriber Optics Gen2 S49', 'Meta Oakley HSTN S51 Black',
                        'Meta Oakley Vanguard Gen 1 Black Size M'],
            'cameras': ['Fujifilm instax Mini Evo Cinema', 'GoPro Max 2 8K 360', 'GoPro Mission 1 Pro',
                        'Insta 360 X6 Standart Bundle', 'Insta 360 GO Ultra 4K White',
                        'Insta 360 AcePro 2 Dual Battery Bundle', 'Insta 360 Link 2C Pro Black',
                        'DJI Osmo Pocket 4P Vlog Combo', 'DJI Osmo 360 II Standard Combo'],
            'consoles': ['Nintendo Switch Lite Gray', 'Sony PlayStation 5 Pro'],
            'accessories': ['Apple USB-C 20W Power Adapter A2347', 'DJI Power 1000 Mini',
                            'DJI Power 200W складная солнечная панель', 'DJI POWER MC4 to XT60 Solar Power Cable',
                            'Xiaomi wired screen display power bank 10000mAh 45W Blue',
                            'Dreame Magnetic Power Bank Air Power 17', 'Galaxy SmartTag2 Black EI-T5600',
                            'Case for Google Pixel Watch 5', 'Чехол для iPhone 17',
                            'Nintendo Switch Joy-Con', 'AirPods Pro Case'],
            'audio': ['Insta 360 Mic Air 1TX + 1RX', 'Insta 360 Wave Arctic White',
                      'Marshall Emberton III Black', 'Яндекс Стрит Зеленая Алиса'],
            'other': ['Sony', 'Samsung', 'DJI', 'Insta360', 'Honor неизвестная позиция',
                      'Netgear ProSafe 48-Port GS752TP', 'Garmin EDGE 540'],
        }
        for expected, names in cases.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(category_for(Product(name)), expected)

    def test_supplied_excerpt_assigns_real_types_without_changing_purchase_prices(self):
        path = Path(__file__).resolve().parent.parent / 'examples' / 'supplier_price_excerpt.txt'
        products = list(parse_price_feed(path.read_text(), require_complete=True).products)
        categories = {product.name: category_for(product) for product in products}
        self.assertEqual(categories['Galaxy Tab A11 4/64 Gray SM-X133'], 'tablets')
        self.assertEqual(categories['Insta 360 Mic Air 1TX + 1RX'], 'audio')
        policy = MarkupPolicy().with_rule('phones', MarkupRule.parse('fixed', '1000'))
        for source, priced in zip(products, price_products(products, policy)):
            expected = source.price + Decimal('1000') if category_for(source) == 'phones' else source.price
            self.assertEqual(priced.price, expected)
