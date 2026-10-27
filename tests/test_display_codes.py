import unittest
from decimal import Decimal

from catalog_bot.catalog import Product, build_reply, display_product_name, find_products
from catalog_bot.conversations import quoted_offers
from catalog_bot.followup_intent import resolve_hold


class DisplayCodesTests(unittest.TestCase):
    def test_manufacturer_codes_are_hidden_without_removing_product_details(self):
        names = {
            'Galaxy S26 Ultra 12/512 Black SM-S948B/DS': 'Galaxy S26 Ultra 12/512 Black',
            'Galaxy Watch9 44mm Silver SM-L350': 'Galaxy Watch9 44mm Silver',
            'Meta Ray-Ban Wayfarer Gen2 S50 RW4012': 'Meta Ray-Ban Wayfarer Gen2 S50',
            'Honor 600 Pro 12/256 Orange VKP-NX9': 'Honor 600 Pro 12/256 Orange',
            'OnePlus 15 12/256 Black CPH2749': 'OnePlus 15 12/256 Black',
            'Apple USB-C 20W Power Adapter A2347': 'Apple USB-C 20W Power Adapter',
            'Galaxy Book 6 NP760VJG - KG1IN': 'Galaxy Book 6',
            '010-04762-01 GARMIN FENIX 9 47 MM': 'GARMIN FENIX 9 47 MM',
            'Sony WH-1000XM6 Black': 'Sony WH-1000XM6 Black',
            'Galaxy S26 Ultra 16/1TB Black': 'Galaxy S26 Ultra 16/1TB Black',
        }
        for name, expected in names.items():
            with self.subTest(name=name):
                self.assertEqual(display_product_name(name), expected)

    def test_search_preserves_code_but_reply_hides_it(self):
        item = Product('Galaxy S26 Ultra 12/512 Black SM-S948B/DS', Decimal('91500'), None, country='🇿🇦')
        _, found = find_products('SM-S948B/DS', [item])
        self.assertEqual(found, [item])
        reply = build_reply(found)
        self.assertIn('Galaxy S26 Ultra 12/512 Black 🇿🇦 — 91 500 ₽', reply)
        self.assertNotIn('SM-S948B/DS', reply)

    def test_new_and_old_quotes_and_copied_rows_work(self):
        item = Product('Galaxy S26 12/256 Black SM-S942B/DS', Decimal('57300'), None, country='🇿🇦')
        for legacy in (False, True):
            row = build_reply([item], intro='', include_codes=legacy)
            with self.subTest(legacy=legacy):
                self.assertEqual(quoted_offers(row, [item]), [item])
                self.assertEqual(resolve_hold('отложи ' + row, [item]), item)

    def test_identical_visible_offers_are_not_reserved_arbitrarily(self):
        a = Product('Galaxy S26 12/256 Black SM-S942B/DS', Decimal('57300'), None, country='🇿🇦')
        b = Product('Galaxy S26 12/256 Black SM-S9420', Decimal('57300'), None, country='🇿🇦')
        row = build_reply([a], intro='')
        self.assertEqual(quoted_offers(row, [a, b]), [a, b])
        self.assertIsNone(resolve_hold('отложи ' + row, [a, b]))
