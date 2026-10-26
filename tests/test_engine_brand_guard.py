import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import Product
from catalog_bot.engine import Engine, Incoming
from catalog_bot.storage import Store


class EngineBrandGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'catalog.sqlite3')
        self.store.toggle_chat(-100123, 'Покупки')
        self.store.set('active', True)
        self.sent = []

        async def send(recipient, source_message_id, text):
            self.sent.append((recipient, source_message_id, text))

        self.engine = Engine(self.store, send, clock=lambda: 1000)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, text):
        return Incoming(-100123, 1, 55, text, timestamp=1000)

    async def test_out_of_stock_iphone_never_falls_back_to_cheaper_xiaomi(self):
        self.store.replace_products([
            Product('iPhone 17 512 Black SIM', Decimal('95000'), 0),
            Product('Xiaomi 17 12/512 Black SIM', Decimal('45000'), 5),
            Product('Redmi Note 17 12/512 Black SIM', Decimal('25000'), 5),
        ])
        result = await self.engine.process(self.message('Куплю айфон 17 512 черный сим'))
        self.assertEqual(result, 'no_match')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.recent(), [])

    async def test_reply_contains_only_requested_brand_and_its_price(self):
        self.store.replace_products([
            Product('iPhone 17 512 Black SIM', Decimal('95000'), 1),
            Product('Xiaomi 17 12/512 Black SIM', Decimal('45000'), 5),
        ])
        self.assertEqual(await self.engine.process(self.message('Куплю айфон 17 512 черный сим')), 'sent')
        self.assertEqual(self.sent[0][:2], (55, 1))
        self.assertIn('iPhone 17', self.sent[0][2])
        self.assertIn('95 000 ₽', self.sent[0][2])
        self.assertNotIn('Xiaomi', self.sent[0][2])
        self.assertNotIn('45 000 ₽', self.sent[0][2])

    async def test_identifier_cannot_force_wrong_brand_into_private_quote(self):
        self.store.replace_products([Product('Xiaomi 17 12/512 Black SIM', Decimal('45000'), 1, sku='IP17')])
        result = await self.engine.process(self.message('Куплю iPhone 17 IP17'))
        self.assertEqual(result, 'no_match')
        self.assertEqual(self.sent, [])
