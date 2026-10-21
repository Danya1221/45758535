import unittest

from catalog_bot.catalog import Product, find_products, inspect_request, parse_spec


class RecognitionIdentityTests(unittest.TestCase):
    def sku(self, query, products):
        return [p.sku for p in find_products(query, products)[1]]

    def test_explicit_iphone_never_crosses_brand(self):
        products = [Product('iPhone 17 256 Black SIM', sku='APPLE17')]
        products += [Product(f'{brand} 17 256 Black SIM', sku=brand.upper())
                     for brand in ('Xiaomi', 'Redmi', 'Poco', 'Honor', 'OnePlus', 'Samsung', 'Pixel')]
        for query in ('Куплю iPhone 17 256 Black SIM', 'Нужен айфон17 256 черный сим'):
            with self.subTest(query=query):
                self.assertEqual(self.sku(query, products), ['APPLE17'])
                self.assertEqual(self.sku(query, products[1:]), [])

    def test_same_numbers_and_price_do_not_override_brand(self):
        products = [Product('Xiaomi 17 256', sku='CHEAP17')]
        self.assertEqual(self.sku('Куплю Apple 17 256', products), [])
        self.assertEqual(self.sku('Куплю iPhone 17 CHEAP17', products), [])
        self.assertEqual(self.sku('Куплю Apple CHEAP17', products), [])

    def test_conflicting_brand_is_rejected_in_request_and_product(self):
        products = [Product('iPhone 17 256', sku='OK'), Product('Xiaomi iPhone 17 256', sku='CONFLICT')]
        self.assertEqual(self.sku('iPhone 17 256', products), ['OK'])
        self.assertEqual(self.sku('Xiaomi iPhone 17 256', products), [])
        self.assertEqual(self.sku('Apple Samsung S26', [Product('Samsung S26', sku='S26')]), [])

    def test_compatibility_does_not_set_accessory_manufacturer(self):
        products = [Product('Apple чехол для iPhone 17', sku='APPLECASE'),
                    Product('Xiaomi чехол для iPhone 17', sku='MICASE')]
        self.assertEqual(self.sku('Куплю Apple чехол для iPhone 17', products), ['APPLECASE'])
        self.assertEqual(self.sku('Куплю Apple MICASE', products), [])
        self.assertEqual(self.sku('Куплю iPhone 17', products), [])
        self.assertIsNone(parse_spec('Чехол для iPhone 17').brand)

    def test_meta_collaboration_is_not_brand_conflict(self):
        products = [Product('Meta RayBan Wayfarer Gen2 Black RW4012', sku='RAY'),
                    Product('Meta Oakley HSTN Gen2 Black', sku='OAK')]
        self.assertEqual(self.sku('Meta RayBan Wayfarer Gen2 Black', products), ['RAY'])
        self.assertEqual(self.sku('RayBan OAK', products), [])

    def test_unknown_brand_is_not_filler(self):
        self.assertEqual(self.sku('MysteryBrand iPhone 17', [Product('iPhone 17', sku='APPLE')]), [])

    def test_different_known_generic_brands_remain_distinct(self):
        products = [Product('Honor 600 12/256 Black', sku='HONOR'),
                    Product('Huawei 600 12/256 Black', sku='HUAWEI')]
        self.assertEqual(self.sku('Honor 600 12/256 Black', products), ['HONOR'])

    def test_related_manufacturer_does_not_merge_distinct_product_lines(self):
        products = [Product('Xiaomi 17 12/256 Black', sku='MI'),
                    Product('Xiaomi Redmi 17 12/256 Black', sku='REDMI'),
                    Product('POCO 17 12/256 Black', sku='POCO')]
        self.assertEqual(self.sku('Xiaomi 17 12/256 Black', products), ['MI'])
        self.assertEqual(self.sku('Redmi 17 12/256 Black', products), ['REDMI'])
        self.assertEqual(self.sku('Xiaomi Redmi 17 12/256 Black', products), ['REDMI'])
        self.assertEqual(self.sku('POCO 17 12/256 Black', products), ['POCO'])
        self.assertEqual(self.sku('Xiaomi Redmi POCO 17', products), [])
        self.assertEqual(self.sku('Note 17 256', [Product('Redmi Note 17 256', sku='RN'),
                                                Product('POCO Note 17 256', sku='PN')]), [])

    def test_fitbit_combined_catalog_label_stays_its_own_family(self):
        products = [Product('Google Fitbit Air Berry Pink', sku='FITBIT'),
                    Product('Google Pixel Watch 5 Berry Pink', sku='PIXELWATCH')]
        self.assertEqual(self.sku('Google Fitbit Air Berry Pink', products), ['FITBIT'])

    def test_brandless_matching_requires_one_brand_in_catalog(self):
        products = [Product('Redmi Note17 256', sku='REDMI'), Product('Honor Note17 256', sku='HONOR')]
        self.assertEqual(self.sku('Note17 256', products), [])
        self.assertEqual(self.sku('Note17 256', products[:1]), ['REDMI'])

    def test_families_with_same_brand_remain_distinct(self):
        products = [Product('iPhone 17 256', sku='PHONE'), Product('iPad 17 256', sku='PAD')]
        self.assertEqual(self.sku('Apple iPhone 17', products), ['PHONE'])
        self.assertEqual(self.sku('iPhone 17 iPad 17', products), [])

    def test_brand_is_in_diagnostics(self):
        report = inspect_request('Apple XCODE', [Product('Xiaomi 17', sku='XCODE')])
        self.assertEqual(report.products, [])
        self.assertIn('бренд', '\n'.join(report.details))


class RecognitionStructureTests(unittest.TestCase):
    def names(self, query, *names):
        return [p.name for p in find_products(query, [Product(n) for n in names])[1]]

    def test_one_sim_forms(self):
        items = ('iPhone 17 256 Black SIM', 'iPhone 17 256 Black eSIM', 'iPhone 17 256 Black 2 SIM')
        for spelling in ('1sim', '1сим', '1 сим'):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.names(f'айфон17 256 черный {spelling}', *items), [items[0]])

    def test_joined_versions_preserve_model_identity(self):
        items = ('iPhone 17 Pro Max 256 Black SIM', 'iPhone 17 Pro 256 Black SIM', 'iPhone 17 256 Black SIM')
        for spelling in ('iPhone17ProMax', 'айфон17промакс', '17пм', '17PM'):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.names(f'{spelling} 256 black sim', *items), [items[0]])

    def test_suffixes_and_model_digits_are_exact(self):
        items = ('OnePlus 15 12/256 Black', 'OnePlus 15R 12/256 Black', 'OnePlus 15T 12/256 Black')
        self.assertEqual(self.names('OnePlus15R 12/256 Black', *items), [items[1]])
        self.assertEqual(self.names('OnePlus15X 12/256 Black', *items), [])
        self.assertEqual(self.names('OnePlus16R 12/256 Black', *items), [])
        self.assertEqual(self.names('Samsung S25 Ultra', 'Samsung S26 Ultra'), [])

    def test_unknown_sim_or_color_is_not_ignored(self):
        self.assertEqual(self.names('iPhone 17 256 3sim', 'iPhone 17 256 SIM'), [])
        self.assertEqual(self.names('iPhone 17 256 Blakish', 'iPhone 17 256 Black'), [])

    def test_unknown_hyphen_quantity_is_not_guessed(self):
        for ending in ('-59', '-1', '- 1'):
            self.assertEqual(self.names(f'iPhone 17 256 {ending}', 'iPhone 17 256'), [])

    def test_conflicting_conditions_are_not_silently_relaxed(self):
        self.assertEqual(self.names('iPhone 17 new used', 'iPhone 17 used'), [])

    def test_normalization_has_a_readable_audit_trail(self):
        report = inspect_request('айфон17промакс 256 черный 1сим', [Product('iPhone 17 Pro Max 256 Black SIM')])
        self.assertIn('Нормализация:', '\n'.join(report.details))

    def test_sku_attributes_are_opaque_but_natural_brand_cannot_be_masked(self):
        used = Product('iPhone 17 used', sku='IP17-USED')
        self.assertEqual(find_products('IP17-USED', [used])[1], [])
        self.assertEqual(find_products('IP17-USED used', [used])[1], [used])
        for sku, query in (('iPhone17', 'iPhone17'), ('APPLE', 'Apple'),
                           ('Samsung-S26', 'Samsung-S26')):
            with self.subTest(sku=sku):
                product = Product('Xiaomi 17 256', sku=sku)
                self.assertEqual(find_products(query, [product])[1], [])

    def test_sku_color_and_version_spellings_do_not_become_equivalent(self):
        items = [Product('iPhone 17 256 Black', sku='IP17-BK'),
                 Product('iPhone 17 256 White', sku='IP17-BLACK'),
                 Product('iPhone 17 Pro Max 256', sku='IP17-PM'),
                 Product('iPhone 17 Pro 256', sku='IP17-PROMAX')]
        for product in items:
            with self.subTest(sku=product.sku):
                self.assertEqual(find_products(product.sku, items)[1], [product])

    def test_watch_case_in_body_description_is_not_a_protective_accessory(self):
        device = 'Google Pixel Watch 5 WI-FI 45mm Polished Silver Case / Fog Active Band'
        accessories = ('Aluminum Case for Google Pixel Watch 5 45mm',
                       'Google Pixel Watch 5 45mm Protective Aluminum Case')
        self.assertEqual(self.names('Pixel Watch 5 45mm', device, *accessories), [device])
