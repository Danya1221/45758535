import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import Product
from catalog_bot.engine import Engine, Incoming
from catalog_bot.offer_selection import select_offers
from catalog_bot.storage import Store


NAME = 'Galaxy S26 Ultra 16/1TB White SM-S948B/DS'


def offer(region, price, **kwargs):
    return Product(NAME, Decimal(str(price)) if price is not None else None, None,
                   country=region, price_country=region, **kwargs)


class OfferSelectionTests(unittest.TestCase):
    def test_cheapest_plus_cheapest_kazakhstan(self):
        ae, cl, py, kz, kz_expensive = [offer(*v) for v in [
            ('🇦🇪', 112700), ('🇨🇱', 113500), ('🇵🇾', 111600), ('🇰🇿', 115000), ('🇰🇿', 116000)]]
        self.assertEqual(select_offers([ae, cl, py, kz, kz_expensive]), [py, kz])

    def test_kazakhstan_cheapest_is_only_shown_once(self):
        kz, ae = offer('🇰🇿', 100), offer('🇦🇪', 120)
        self.assertEqual(select_offers([kz, ae]), [kz])

    def test_different_configurations_and_identifiers_are_never_merged(self):
        base = offer('🇦🇪', 100)
        differences = [replace(offer('🇵🇾', 90), name=NAME.replace('White', 'Black')),
                       replace(offer('🇵🇾', 90), name=NAME.replace('1TB', '512')),
                       replace(offer('🇵🇾', 90), name=NAME + ' eSIM'),
                       replace(offer('🇵🇾', 90), name=NAME.replace('S948B', 'S9480')),
                       replace(offer('🇵🇾', 90), sku='other-sku')]
        self.assertEqual(select_offers([base, *differences]), [base, *differences])

    def test_unknown_prices_are_not_assumed_more_expensive(self):
        known, unknown = offer('🇵🇾', 100), offer('🇦🇪', None)
        self.assertEqual(select_offers([known, unknown]), [known, unknown])


class OfferEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'test.sqlite3')
        self.store.set('active', True)
        self.store.set('telegram_account_id', 77)
        self.store.toggle_chat(-100, 'test')
        self.sent = []

        async def send(recipient, source, text):
            self.sent.append(text)
            return [900]

        self.engine = Engine(self.store, send, clock=lambda: 1000)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_customer_and_recorded_quote_get_only_selected_offers(self):
        self.store.replace_products([offer('🇦🇪', 112700), offer('🇨🇱', 113500),
                                     offer('🇵🇾', 111600), offer('🇰🇿', 115000)])
        message = Incoming(-100, 1, 55, 'Samsung Galaxy S26 Ultra 1TB White', 1000)
        self.assertEqual(await self.engine.process(message), 'sent')
        text = self.sent[0]
        self.assertIn('🇵🇾 — 111 600 ₽', text)
        self.assertIn('🇰🇿 — 115 000 ₽', text)
        self.assertNotIn('🇦🇪', text)
        self.assertNotIn('🇨🇱', text)
        quote = self.store.quote_for_reply(77, 55, 900, now=1000)
        self.assertEqual({p.country for p in quote['products']}, {'🇵🇾', '🇰🇿'})

    async def test_region_explicitly_requested_is_not_replaced_by_another(self):
        self.store.replace_products([offer('🇦🇪', 112700), offer('🇵🇾', 111600), offer('🇰🇿', 115000)])
        message = Incoming(-100, 1, 55, 'Samsung Galaxy S26 Ultra 1TB White 🇦🇪', 1000)
        self.assertEqual(await self.engine.process(message), 'sent')
        self.assertIn('🇦🇪', self.sent[0])
        self.assertNotIn('🇵🇾', self.sent[0])
        self.assertNotIn('🇰🇿', self.sent[0])
