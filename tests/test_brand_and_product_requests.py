"""Representative real-world product requests and their safety boundaries."""
import unittest
from decimal import Decimal

from catalog_bot.catalog import Product, find_products


class BrandAndProductRequestsTests(unittest.TestCase):
    def setUp(self):
        names = [
            'iPhone 16 Pro Max 512 Black',
            'iPhone 17 256 Black',
            'iPhone 17 Pro 512 Black',
            'Apple USB-C 20W Power Adapter A2347',
            'Apple iPhone 17 Black adapter',
            'iPhone 17 Black Magnetic Battery',
            'Чехол для iPhone 17 Black',
            'Redmi 17 6/128 Deep Blue NFC',
            'POCO F9 Pro 12/256 Green',
            'Xiaomi Pad 7 8/256 Gray',
            'Google Pixel 11 256 Obsidian',
            'Google Pixel 11 Pro 512 Fog',
            'Google Pixel Watch 5 41mm Black',
            'Nintendo Switch Lite Gray',
            'Nintendo Switch Lite Coral',
            'Sony WH-1000XM6 Black',
            'Sony WH-1000XM6 Silver',
            'GoPro Max 2 8K 360',
            'GoPro Max 1 8K 360',
            'Insta 360 X5 - 8K 360 Action Cam Satin White',
            'Insta 360 X6 - Essentials Bundle Midnight Black',
            'Oura Ring 5 - Size 8 Black',
            'Oura Ring 5 - Size 9 Silver',
            'Oura Ring 6 - Size 8 Black',
            '010-04762-01 GARMIN FENIX 9 47 MM BARE TI PEBBLE GRAY BAND',
            '010-04763-01 GARMIN FENIX 9 51 MM AMOLED PEBBLE GRAY',
            '010-02694-04 GARMIN EDGE 540',
            'Fujifilm instax Mini Evo Cinema',
            'Instax mini 13 Dreamy Blue',
            'OnePlus 15 5G 16/512 Ultra Violet',
            'OnePlus 15 12/256 Infinite Black CPH2749',
            'OnePlus 15 Pro 16/512 Black',
            'Meta Quest 3 128GB',
            'Meta Ray-Ban Wayfarer Gen2 S50 Black',
            'Meta Ray-Ban Wayfarer Gen2 Lens Kit Black',
            'Meta Ray-Ban Wayfarer Gen2 charging pouch',
            'Meta Oakley HSTN S51 Black',
            'Ray-Ban Skyler Gen2 S52 Black',
            'Samsung Galaxy S26 Ultra 12/512 Black SM-S948B/DS',
        ]
        self.products = [Product(name, Decimal(10000 + i), None) for i, name in enumerate(names)]

    def names(self, query):
        return {product.name for product in find_products(query, self.products)[1]}

    def test_brand_requests_return_their_available_devices_only(self):
        cases = {
            'iphone': {'iPhone 16 Pro Max 512 Black', 'iPhone 17 256 Black', 'iPhone 17 Pro 512 Black'},
            'айфон': {'iPhone 16 Pro Max 512 Black', 'iPhone 17 256 Black', 'iPhone 17 Pro 512 Black'},
            'redmi': {'Redmi 17 6/128 Deep Blue NFC'},
            'poco': {'POCO F9 Pro 12/256 Green'},
            'pixel': {'Google Pixel 11 256 Obsidian', 'Google Pixel 11 Pro 512 Fog'},
            'nintendo': {'Nintendo Switch Lite Gray', 'Nintendo Switch Lite Coral'},
            'sony': {'Sony WH-1000XM6 Black', 'Sony WH-1000XM6 Silver'},
            'gopro': {'GoPro Max 2 8K 360', 'GoPro Max 1 8K 360'},
            'meta': {'Meta Quest 3 128GB', 'Meta Ray-Ban Wayfarer Gen2 S50 Black', 'Meta Oakley HSTN S51 Black'},
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.names(query), expected)

    def test_iphone_exact_models_do_not_mix_generations_or_accessories(self):
        self.assertEqual(self.names('iphone 16 pro max 512 black'), {'iPhone 16 Pro Max 512 Black'})
        self.assertEqual(self.names('iphone 17 256 black'), {'iPhone 17 256 Black'})
        self.assertEqual(self.names('iphone 17 pro 512 black'), {'iPhone 17 Pro 512 Black'})
        self.assertEqual(self.names('iphone 18 pro max 512 black'), set())

    def test_ring_size_is_not_the_generation(self):
        self.assertEqual(self.names('Oura Ring 5'), {'Oura Ring 5 - Size 8 Black', 'Oura Ring 5 - Size 9 Silver'})
        self.assertEqual(self.names('Oura Ring 5 Size 8'), {'Oura Ring 5 - Size 8 Black'})
        self.assertEqual(self.names('Oura Ring 5 Size 9'), {'Oura Ring 5 - Size 9 Silver'})
        self.assertEqual(self.names('Oura Ring 6 Size 8'), {'Oura Ring 6 - Size 8 Black'})
        self.assertEqual(self.names('Oura Ring 6 Size 9'), set())

    def test_garmin_supplier_code_is_searchable_but_not_a_generation(self):
        fenix47 = '010-04762-01 GARMIN FENIX 9 47 MM BARE TI PEBBLE GRAY BAND'
        fenix51 = '010-04763-01 GARMIN FENIX 9 51 MM AMOLED PEBBLE GRAY'
        edge = '010-02694-04 GARMIN EDGE 540'
        self.assertEqual(self.names('Garmin Fenix 9'), {fenix47, fenix51})
        self.assertEqual(self.names('010-04762-01'), {fenix47})
        self.assertEqual(self.names('Garmin Edge 540'), {edge})
        self.assertEqual(self.names('Garmin Fenix 8'), set())

    def test_camera_360_feature_does_not_change_model_or_generation(self):
        x5 = 'Insta 360 X5 - 8K 360 Action Cam Satin White'
        x6 = 'Insta 360 X6 - Essentials Bundle Midnight Black'
        self.assertEqual(self.names('Insta360 X5'), {x5})
        self.assertEqual(self.names('Insta360 X6'), {x6})
        self.assertEqual(self.names('GoPro Max 2'), {'GoPro Max 2 8K 360'})
        self.assertEqual(self.names('GoPro Max 1'), {'GoPro Max 1 8K 360'})
        self.assertEqual(self.names('GoPro Max 3'), set())

    def test_named_camera_line_allows_a_specific_bundle(self):
        self.assertEqual(self.names('Fujifilm instax Mini Evo'), {'Fujifilm instax Mini Evo Cinema'})
        self.assertEqual(self.names('instax mini 13'), {'Instax mini 13 Dreamy Blue'})

    def test_oneplus_ultra_violet_is_a_color_not_an_ultra_model(self):
        violet = 'OnePlus 15 5G 16/512 Ultra Violet'
        self.assertEqual(self.names('OnePlus 15 16/512 Ultra Violet'), {violet})
        self.assertEqual(self.names('OnePlus 15 512 purple'), {violet})
        self.assertEqual(self.names('OnePlus 15 512 black'), set())

    def test_sony_xm_short_request_and_wh_prefix_preserve_colors(self):
        black = 'Sony WH-1000XM6 Black'
        silver = 'Sony WH-1000XM6 Silver'
        self.assertEqual(self.names('sony xm6'), {black, silver})
        self.assertEqual(self.names('sony xm5'), set())
        self.assertEqual(self.names('sony xm6 black'), {black})
        self.assertEqual(self.names('sony wh-1000xm6 white'), set())
        self.assertEqual(self.names('sony wh 1000xm6 black'), {black})

    def test_accessories_do_not_appear_as_phones_or_glasses(self):
        self.assertEqual(self.names('iphone 17'), {'iPhone 17 256 Black'})
        self.assertEqual(self.names('Ray-Ban'), {'Meta Ray-Ban Wayfarer Gen2 S50 Black',
                                                  'Ray-Ban Skyler Gen2 S52 Black'})
        self.assertEqual(self.names('Meta'), {'Meta Quest 3 128GB', 'Meta Ray-Ban Wayfarer Gen2 S50 Black',
                                               'Meta Oakley HSTN S51 Black'})

    def test_cameras_with_battery_bundles_remain_cameras(self):
        camera = Product('Insta 360 AcePro 2 Dual Battery Bundle', Decimal('27000'), None)
        self.assertEqual(find_products('Insta360 AcePro 2', [camera])[1], [camera])


if __name__ == '__main__':
    unittest.main()
