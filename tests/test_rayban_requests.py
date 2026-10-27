import unittest
from decimal import Decimal

from catalog_bot.catalog import Product, find_products, build_reply


class RayBanRequestsTests(unittest.TestCase):
    def setUp(self):
        self.wayfarer = Product('Meta Ray-Ban Wayfarer Gen2 S50 (F Matte Black, L Transitions grey) RW4012', Decimal('36000'), None)
        self.skyler = Product('Meta Ray-Ban Skyler Gen2 S52 (F Shiny Black, L G15 Green) RW4014', Decimal('29500'), None)
        self.gen1 = Product('Meta Ray-Ban Wayfarer Gen1 S50 Black RW4001', Decimal('25000'), None)
        self.ordinary = Product('Ray-Ban Wayfarer Gen2 S50 Black RW4002', Decimal('20000'), None)
        self.oakley = Product('Meta Oakley HSTN S51 Black 00W8002', Decimal('30000'), None)
        self.accessory = Product('Meta Ray-Ban чехол Wayfarer Black', Decimal('1000'), None)
        self.sold = Product('Meta Ray-Ban Skyler Gen2 S52 Blue RW4015', Decimal('25000'), 0)
        self.items = [self.wayfarer, self.skyler, self.gen1, self.ordinary, self.oakley, self.accessory, self.sold]

    def matches(self, text):
        return find_products(text, self.items)[1]

    def test_bare_brand_and_russian_aliases_return_rayban_glasses(self):
        for text in ('ray-ban', 'Ray Ban', 'рейбан', 'райбаны', 'рейбаны'):
            with self.subTest(text=text):
                self.assertEqual(self.matches(text), [self.wayfarer, self.skyler, self.gen1, self.ordinary])

    def test_meta_only_returns_meta_rayban_glasses(self):
        for text in ('Ray-Ban Meta.', 'мета Ray-Ban.', 'Куплю очки Meta Ray Ban'):
            with self.subTest(text=text):
                found = self.matches(text)
                self.assertEqual(found, [self.wayfarer, self.skyler, self.gen1])
                reply = build_reply(found)
                self.assertIn('36 000 ₽', reply)
                self.assertNotIn('RW4012', reply)

    def test_specific_frame_generation_color_and_size_are_respected(self):
        self.assertEqual(self.matches('Meta Ray-Ban Wayfarer'), [self.wayfarer, self.gen1])
        self.assertEqual(self.matches('Meta Ray-Ban Wayfarer Gen2'), [self.wayfarer])
        self.assertEqual(self.matches('Meta Ray-Ban Skyler Green'), [self.skyler])
        self.assertEqual(self.matches('Meta Ray-Ban Wayfarer S52'), [])
        self.assertEqual(self.matches('Meta Ray-Ban Wayfarer Gen3'), [])

    def test_brand_conflict_unknown_line_and_sales_never_trigger(self):
        for text in ('Ray-Ban Oakley', 'Ray-Ban Quest', 'продам Meta Ray-Ban', 'Samsung', 'Meta'):
            with self.subTest(text=text):
                self.assertEqual(self.matches(text), [])
