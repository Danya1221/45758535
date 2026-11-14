import unittest
import tempfile
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import Product, find_products, inspect_request, parse_catalog, parse_spec
from catalog_bot.offer_selection import select_offers
from catalog_bot.prices import parse_price_feed
from catalog_bot.storage import Store
from catalog_bot.engine import Engine, Incoming


def offer(sku, name, price='100000'):
    return Product(name=name, price=Decimal(price), sku=sku)


class IphoneDuoTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            offer('WHITE256', '18 Duo 256 White (eSim)'),
            offer('WHITE512', 'iPhone 18 Duo 512 White eSIM'),
            offer('NIGHT256', '18 Duo 256 Night Sky (eSim)'),
            offer('ACTIVE256', '18 Duo 256 Night Sky (eSim) Актив'),
            offer('NIGHT2TB', '18 Duo 2TB Night Sky (eSim)'),
            offer('SIM256', 'iPhone 18 Duo 256 White 1 SIM'),
            offer('OTHERGEN', 'iPhone 17 Duo 256 White eSIM'),
            offer('BASE18', 'iPhone 18 256 White eSIM'),
            offer('PRO18', 'iPhone 18 Pro 256 White eSIM'),
            offer('OTHERBRAND', 'Xiaomi 18 Duo 256 White eSIM'),
        ]

    def ids(self, query):
        return [p.sku for p in find_products(query, self.items)[1]]

    def test_exact_duo_in_english_and_russian(self):
        for query in ('iPhone 18 Duo 256GB White eSIM', 'iPhone Duo 18 256 White eSIM',
                      'айфон 18 дуо 256 белый есим'):
            with self.subTest(query=query):
                self.assertEqual(self.ids(query), ['WHITE256'])
        without_conflicting_brand = [p for p in self.items if p.sku != 'OTHERBRAND']
        for query in ('18 Duo 256 White eSIM', '18дуо 256 белый есим'):
            with self.subTest(query=query):
                self.assertEqual([p.sku for p in find_products(query, without_conflicting_brand)[1]], ['WHITE256'])
                self.assertEqual(self.ids(query), [])  # two possible manufacturers, do not guess

    def test_night_sky_is_not_white_or_activated(self):
        for query in ('iPhone 18 Duo 256 night sky esim', '18 дуо 256 найт скай есим'):
            with self.subTest(query=query):
                self.assertEqual(self.ids(query), ['NIGHT256'])
        self.assertEqual(self.ids('18 Duo 256 Night Sky eSIM актив'), ['ACTIVE256'])

    def test_memory_sim_and_model_variants_are_strict(self):
        self.assertEqual(self.ids('айфон 18 дуо 512 белый есим'), ['WHITE512'])
        self.assertEqual(self.ids('iPhone 18 Duo 2TB Night Sky eSIM'), ['NIGHT2TB'])
        self.assertEqual(self.ids('18 Duo 256 White 1 sim'), ['SIM256'])
        self.assertEqual(self.ids('iPhone 18 Pro 256 white esim'), ['PRO18'])
        self.assertEqual(self.ids('iPhone 18 256 white esim'), ['BASE18'])

    def test_explicit_iphone_duo_without_generation_stays_with_duo(self):
        self.assertEqual(self.ids('айфон дуо 256 белый есим'), ['WHITE256', 'OTHERGEN'])
        self.assertEqual(self.ids('iPhone Duo 512'), ['WHITE512'])
        self.assertEqual(self.ids('дуо 256'), [])

    def test_conflicting_qualifiers_and_accessories_are_not_matches(self):
        self.assertEqual(self.ids('iPhone 18 Duo Pro 256 White eSIM'), [])
        self.assertEqual(self.ids('iPhone 18 Duo 256 White 2 sim'), [])
        self.assertEqual(self.ids('Xiaomi 18 Duo 256 White eSIM'), ['OTHERBRAND'])
        self.assertEqual(find_products('чехол для iPhone 18 Duo', self.items)[1], [])

    def test_real_price_formats_and_diagnostics(self):
        feed = parse_price_feed('• 18 Duo 256 White (eSim)\n🇺🇸 От 1 шт - 194 300\n'
                                '• 18 Duo 256 Night Sky (eSim)\n🇺🇸 От 1 шт - 195 300\n'
                                'Всего найдено 2 товаров', require_complete=True)
        own = parse_catalog('iPhone 18 Duo 256 White eSIM|195000|3')
        report = inspect_request('18 дуо 256 белый есим', list(feed.products) + own)
        self.assertEqual(len(report.products), 2)
        self.assertIn('duo', ' '.join(report.details))
        self.assertEqual(parse_spec(feed.products[0].name).variants, frozenset({'duo'}))
        self.assertEqual(select_offers(report.products)[0].price, Decimal('194300'))


class DuoEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_buyer_request_gets_matching_duo_price_from_uploaded_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'bot.sqlite3')
            snapshot = parse_price_feed('• 18 Duo 256 White (eSim)\n🇺🇸 От 1 шт - 194 300\n'
                                        '• iPhone 18 Pro 256 White (eSim)\n🇺🇸 От 1 шт - 100 000\n'
                                        'Всего найдено 2 товаров', require_complete=True)
            store.apply_price_snapshot(snapshot, 'bot:123')
            store.toggle_chat(-123, 'test')
            store.set('active', True)
            sent = []
            async def send(user_id, message_id, text):
                sent.append((user_id, text))
                return []
            engine = Engine(store, send, clock=lambda: 1000)
            result = await engine.process(Incoming(-123, 1, 42, 'айфон 18 дуо 256 белый есим', 1000))
            self.assertEqual(result, 'sent')
            self.assertEqual(sent[0][0], 42)
            self.assertIn('18 Duo 256 White', sent[0][1])
            self.assertIn('194 300 ₽', sent[0][1])
            self.assertNotIn('18 Pro', sent[0][1])
            store.close()
