import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import Product, build_reply, find_products, parse_spec
from catalog_bot.price_sync import PriceCollector
from catalog_bot.prices import IncompletePrice, PriceFeedError, parse_price_feed
from catalog_bot.storage import Store

EXCERPT = Path(__file__).resolve().parent.parent / 'examples' / 'supplier_price_excerpt.txt'


def price_text(price='60 000', extra='', total=1):
    return f'**Цены на Все товары**\n**• Galaxy S26 12/256 Black SM-S942B/DS🇿🇦**\n🇿🇦 От 1 шт - {price}\n{extra}\nВсего найдено {total} товаров'


class PriceParsingTests(unittest.TestCase):
    def test_real_format_excerpt_duplicates_and_conflicts(self):
        snapshot = parse_price_feed(EXCERPT.read_text(), require_complete=True)
        self.assertEqual(snapshot.raw_count, 14)
        self.assertEqual(len(snapshot.products), 13)
        self.assertEqual(snapshot.duplicates, 1)
        self.assertEqual(sum(bool(p.warnings) for p in snapshot.products), 1)

    def test_price_threshold_does_not_become_stock(self):
        product = parse_price_feed(price_text()).products[0]
        self.assertEqual(product.price, Decimal('60000'))
        self.assertIsNone(product.stock)
        self.assertEqual(product.min_qty, 1)
        self.assertNotIn('в наличии', build_reply([product]))
        self.assertIn('🇿🇦', build_reply([product]))

    def test_regions_are_distinct_offers(self):
        snapshot = parse_price_feed(EXCERPT.read_text())
        found = find_products('Куплю S26 12/256 Black', list(snapshot.products))[1]
        self.assertEqual(len(found), 2)
        self.assertEqual({p.price for p in found}, {Decimal('60000'), Decimal('59000')})
        self.assertEqual(len(find_products('Куплю S26 12/256 Black 🇭🇰', list(snapshot.products))[1]), 1)

    def test_conflicting_region_is_not_quoted(self):
        snapshot = parse_price_feed(EXCERPT.read_text())
        self.assertEqual(find_products('Куплю Pixel 11 Pro Fold 512 Obsidian', list(snapshot.products))[1], [])

    def test_pro_xl_and_fold_are_separate(self):
        snapshot = parse_price_feed(EXCERPT.read_text())
        found = find_products('Куплю Pixel 11 Pro 256 Obsidian', list(snapshot.products))[1]
        self.assertEqual(len(found), 1)
        self.assertNotIn('XL', found[0].name)
        self.assertEqual(len(find_products('Куплю Pixel 11 Pro XL 256 Obsidian', list(snapshot.products))[1]), 1)

    def test_tablet_and_glasses_do_not_match_phone_model(self):
        products = list(parse_price_feed(EXCERPT.read_text()).products)
        self.assertEqual(find_products('Куплю A11', products)[1], [])
        self.assertEqual(find_products('Куплю S50', products)[1], [])

    def test_ram_terabyte_and_no_bullet(self):
        products = list(parse_price_feed(EXCERPT.read_text()).products)
        self.assertEqual(parse_spec('Honor Magic 8 Pro 16/1TB Sunrise Gold').ram, 16)
        self.assertEqual(len(find_products('Куплю Honor Magic 8 Pro 16/1TB Sunrise Gold', products)[1]), 1)
        self.assertTrue(any(p.name.startswith('Galaxy S26+') for p in products))

    def test_incomplete_total_never_accepted(self):
        with self.assertRaises(IncompletePrice):
            parse_price_feed(price_text(total=425), require_complete=True)

    def test_no_footer_can_only_be_partial(self):
        text = price_text().split('Всего')[0]
        self.assertFalse(parse_price_feed(text).complete)
        with self.assertRaises(IncompletePrice):
            parse_price_feed(text, require_complete=True)

    def test_duplicate_with_conflicting_price_rejected(self):
        extra = '**• Galaxy S26 12/256 Black SM-S942B/DS🇿🇦**\n🇿🇦 От 1 шт - 61 000'
        with self.assertRaises(PriceFeedError):
            parse_price_feed(price_text(extra=extra, total=2))

    def test_quantity_tiers_are_not_single_unit_prices(self):
        text = '**• iPhone 17 256 Black**\n🇨🇳 От 5 шт - 80 000\nВсего найдено 1 товаров'
        product = parse_price_feed(text).products[0]
        self.assertEqual(product.min_qty, 5)
        self.assertEqual(find_products('Куплю iPhone 17', [product])[1], [])

    def test_multiple_color_requirements_are_not_treated_as_alternatives(self):
        products = [Product('Meta RayBan Wayfarer Gen2 Black Green'), Product('Meta RayBan Wayfarer Gen2 Black Gray')]
        found = find_products('Куплю RayBan Wayfarer Gen2 Black Gray', products)[1]
        self.assertEqual(len(found), 1)
        self.assertIn('Gray', found[0].name)

    def test_nbsp_and_missing_final_price(self):
        self.assertEqual(parse_price_feed(price_text(price='60\u00a0000')).products[0].price, Decimal('60000'))
        with self.assertRaises(IncompletePrice):
            parse_price_feed('• iPhone 17\nВсего найдено 1 товаров')

    def test_explicit_complete_empty_catalog(self):
        snapshot = parse_price_feed('Цены на Все товары\nВсего найдено 0 товаров', require_complete=True)
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.products, ())


class PriceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'catalog.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_supplier_refresh_keeps_replies_free_of_automatic_headings(self):
        self.store.apply_price_snapshot(parse_price_feed(price_text()), 'bot:123')
        self.assertEqual(self.store.get('intro'), '')
        self.assertTrue(build_reply(self.store.products(), self.store.get('intro')).startswith('• Galaxy S26'))
        self.store.apply_price_snapshot(parse_price_feed(price_text(price='61 000')), 'bot:123')
        self.assertEqual(self.store.get('intro'), '')

    def test_old_supplier_heading_is_removed_after_restart_but_custom_intro_survives(self):
        self.store.set('intro', 'По актуальному прайсу:')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get('intro'), '')
        self.store.set('intro', 'Моё предложение:')
        self.store.apply_price_snapshot(parse_price_feed(price_text()), 'bot:123')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get('intro'), 'Моё предложение:')

    def test_price_change_uses_same_offer_and_persists(self):
        first = self.store.apply_price_snapshot(parse_price_feed(price_text()), 'bot:123')
        self.assertTrue(first['baseline'])
        second = self.store.apply_price_snapshot(parse_price_feed(price_text(price='61 500')), 'bot:123')
        self.assertEqual(second['changed'], 1)
        self.assertEqual(second['added'], 0)
        self.assertEqual(second['removed'], 0)
        self.store.close()
        self.store = Store(self.path)
        run, changes = self.store.latest_price_run()
        self.assertEqual(changes[0]['old_price'], '60000')
        self.assertEqual(changes[0]['new_price'], '61500')
        self.assertIsNone(self.store.products()[0].stock)

    def test_region_not_merged_with_cheapest_price(self):
        self.store.apply_price_snapshot(parse_price_feed(EXCERPT.read_text()), 'bot:123')
        region_prices = {p.country: p.price for p in self.store.products() if p.name.startswith('Galaxy S26 ')}
        self.assertEqual(region_prices['🇿🇦'], Decimal('60000'))
        self.assertEqual(region_prices['🇭🇰'], Decimal('59000'))

    def test_partial_update_never_removes_missing_products(self):
        self.store.apply_price_snapshot(parse_price_feed(EXCERPT.read_text()), 'bot:123')
        partial = parse_price_feed(price_text('62 000').split('Всего')[0])
        summary = self.store.apply_price_snapshot(partial, 'bot:123')
        self.assertEqual(summary['removed'], 0)
        self.assertEqual(summary['total'], 13)
        self.assertEqual(summary['changed'], 1)

    def test_complete_update_removes_disappeared_offers(self):
        self.store.apply_price_snapshot(parse_price_feed(EXCERPT.read_text()), 'bot:123')
        summary = self.store.apply_price_snapshot(parse_price_feed(price_text()), 'bot:123')
        self.assertEqual(summary['removed'], 12)
        self.assertEqual(summary['total'], 1)

    def test_partial_new_source_cannot_discard_old_catalog(self):
        self.store.apply_price_snapshot(parse_price_feed(EXCERPT.read_text()), 'bot:123')
        with self.assertRaises(PriceFeedError):
            self.store.apply_price_snapshot(parse_price_feed(price_text().split('Всего')[0]), 'bot:456')
        self.assertEqual(len(self.store.products()), 13)

    def test_legacy_database_migrates_without_losing_products(self):
        self.store.close()
        legacy = sqlite3.connect(self.path)
        legacy.execute('DROP TABLE products')
        legacy.execute('CREATE TABLE products (id INTEGER PRIMARY KEY,name TEXT NOT NULL,price TEXT,stock INTEGER NOT NULL,sku TEXT NOT NULL)')
        legacy.execute("INSERT INTO products VALUES (1,'iPhone 17','85000',2,'CODE')")
        legacy.commit()
        legacy.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.products()[0].stock, 2)
        self.assertEqual(self.store.products()[0].country, '')


class CollectorTests(unittest.TestCase):
    def test_synthetic_425_product_snapshot_across_many_messages(self):
        collector = PriceCollector()
        collector.begin(1)
        for chunk in range(17):
            text = '\n\n'.join(f'• Тестовый товар Model{i}🇨🇳\n🇨🇳 От 1 шт - {10000+i}' for i in range(chunk*25, (chunk+1)*25))
            self.assertIsNone(collector.add(chunk+1, text))
        snapshot = collector.add(18, 'Всего найдено 425 товаров')
        self.assertEqual(snapshot.raw_count, 425)
        self.assertEqual(len(snapshot.products), 425)

    def test_chunks_can_arrive_out_of_order(self):
        collector = PriceCollector()
        collector.begin(10)
        self.assertIsNone(collector.add(12, 'Всего найдено 1 товаров'))
        result = collector.add(11, price_text().split('Всего')[0])
        self.assertTrue(result.complete)

    def test_stale_chunks_are_ignored_and_edits_replace(self):
        collector = PriceCollector()
        collector.begin(10, edit_id=5)
        self.assertIsNone(collector.add(4, price_text()))
        self.assertIsNone(collector.add(5, 'Подождите'))
        result = collector.add(5, price_text())
        self.assertEqual(result.products[0].price, Decimal('60000'))


if __name__ == '__main__':
    unittest.main()
