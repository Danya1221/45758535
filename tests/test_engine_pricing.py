import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from catalog_bot.catalog import Product
from catalog_bot.engine import Engine, Incoming
from catalog_bot.prices import parse_price_feed
from catalog_bot.pricing import MarkupPolicy, MarkupRule
from catalog_bot.storage import Store


def fixed(value):
    return MarkupRule('fixed', Decimal(value))


def percent(value):
    return MarkupRule('percent', Decimal(value))


class EnginePricingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'catalog.sqlite3'
        self.store = Store(self.path)
        self.store.replace_products([
            Product('Apple iPhone 17 256GB Black SIM', Decimal('85000'), 1, sku='IPHONE'),
            Product('Xiaomi 17 12/256GB Black SIM', Decimal('65000'), 1, sku='XIAOMI'),
            Product('Sony WH-1000XM6 Black', Decimal('25200'), 1, sku='HEADPHONES'),
        ])
        self.store.toggle_chat(-100123, 'Покупки')
        self.store.set('active', True)
        self.sent = []
        self.sequence = 0

        async def send(chat_id, message_id, text):
            self.sent.append((chat_id, message_id, text))

        self.engine = Engine(self.store, send, dedup_seconds=0, clock=lambda: 1000)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def incoming(self, query='Куплю iPhone 17 256 Black SIM'):
        self.sequence += 1
        return Incoming(-100123, self.sequence, 55, query, timestamp=1000)

    def policy(self, default, **overrides):
        self.store.set_markup_policy(MarkupPolicy(default=default, overrides=overrides))

    async def test_default_zero_policy_keeps_source_price(self):
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertIn('85 000 ₽', self.sent[-1][2])
        self.assertEqual(self.store.products()[0].price, Decimal('85000'))

    async def test_fixed_markup_budget_uses_customer_price_and_exact_boundary(self):
        self.policy(fixed('2500'))
        self.assertEqual(await self.engine.process(self.incoming('Куплю iPhone 17 до 86 000')), 'no_match')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.recent(), [])
        self.assertEqual(await self.engine.process(self.incoming('Куплю iPhone 17 до 87 500')), 'sent')
        self.assertIn('87 500 ₽', self.sent[-1][2])
        self.assertNotIn('85 000 ₽', self.sent[-1][2])
        self.assertEqual(self.store.products()[0].price, Decimal('85000'))

    async def test_percent_rounding_and_budget_share_the_same_final_amount(self):
        self.store.replace_products([Product('iPhone 17 256 Black SIM', Decimal('100.05'), 1)])
        self.policy(percent('10'))
        self.assertEqual(await self.engine.process(self.incoming('Куплю iPhone 17 до 110 руб')), 'no_match')
        self.assertEqual(self.sent, [])
        self.assertEqual(await self.engine.process(self.incoming('Куплю iPhone 17 до 111 руб')), 'sent')
        self.assertIn('110,06 ₽', self.sent[-1][2])
        self.assertEqual(self.store.products()[0].price, Decimal('100.05'))

    async def test_category_override_replaces_default_and_does_not_affect_iphone(self):
        self.policy(percent('10'), headphones=fixed('500'))
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertIn('93 500 ₽', self.sent[-1][2])
        self.assertEqual(await self.engine.process(self.incoming('Куплю Sony WH-1000XM6 Black')), 'sent')
        self.assertIn('25 700 ₽', self.sent[-1][2])
        self.assertNotIn('28 220 ₽', self.sent[-1][2])
        self.assertEqual([p.price for p in self.store.products()],
                         [Decimal('85000'), Decimal('65000'), Decimal('25200')])

    async def test_zero_category_override_cancels_default(self):
        self.policy(percent('10'), phones=fixed('0'))
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertIn('85 000 ₽', self.sent[-1][2])
        self.assertEqual(await self.engine.process(self.incoming('Куплю Sony WH-1000XM6 Black')), 'sent')
        self.assertIn('27 720 ₽', self.sent[-1][2])

    async def test_repeated_requests_and_restart_do_not_compound_markup(self):
        self.policy(percent('10'))
        version = self.store.get('pricing_version')
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.store.close()
        self.store = Store(self.path)
        self.engine.store = self.store
        self.assertEqual(self.store.get('pricing_version'), version)
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        for _, _, text in self.sent:
            self.assertIn('93 500 ₽', text)
            self.assertNotIn('102 850 ₽', text)
        self.assertEqual(self.store.products()[0].price, Decimal('85000'))

    async def test_supplier_price_refresh_reprices_from_new_base_and_preserves_history(self):
        def snapshot(price):
            return parse_price_feed('• iPhone 17 256 Black SIM🇺🇸\n'
                                    f'🇺🇸 От 1 шт - {price}\nВсего найдено 1 товаров', require_complete=True)
        self.store.apply_price_snapshot(snapshot('85 000'), 'bot:123')
        self.policy(fixed('2500'))
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertIn('87 500 ₽', self.sent[-1][2])
        self.store.apply_price_snapshot(snapshot('90 000'), 'bot:123')
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertIn('92 500 ₽', self.sent[-1][2])
        self.assertNotIn('95 000 ₽', self.sent[-1][2])
        self.assertEqual(self.store.products()[0].price, Decimal('90000'))
        _, changes = self.store.latest_price_run()
        self.assertEqual(changes[0]['old_price'], '85000')
        self.assertEqual(changes[0]['new_price'], '90000')

    async def test_unknown_source_price_never_becomes_the_fixed_markup(self):
        self.store.replace_products([Product('iPhone 17 256 Black SIM', None, 1)])
        self.policy(fixed('2500'))
        self.assertEqual(await self.engine.process(self.incoming('Куплю iPhone 17 до 100 000')), 'no_match')
        self.assertEqual(self.sent, [])
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertNotIn('₽', self.sent[-1][2])
        self.assertIsNone(self.store.products()[0].price)

    async def test_policy_change_during_matching_cancels_old_quote(self):
        self.policy(fixed('2500'))
        original_version = self.store.get('pricing_version')

        async def changing_policy(function, *args, **kwargs):
            result = function(*args, **kwargs)
            self.policy(fixed('5000'))
            return result

        with patch('catalog_bot.engine.asyncio.to_thread', changing_policy):
            result = await self.engine.process(self.incoming())
        self.assertEqual(result, 'cancelled')
        self.assertGreater(self.store.get('pricing_version'), original_version)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.recent(), [])
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertIn('90 000 ₽', self.sent[-1][2])

    async def test_markup_never_turns_xiaomi_into_iphone_fallback(self):
        self.store.replace_products([
            Product('Apple iPhone 17 256GB Black SIM', Decimal('85000'), 0),
            Product('Xiaomi 17 12/256GB Black SIM', Decimal('65000'), 5),
        ])
        self.policy(fixed('2500'))
        self.assertEqual(await self.engine.process(self.incoming()), 'no_match')
        self.assertEqual(self.sent, [])

    async def test_failed_policy_write_rolls_back_policy_and_version_together(self):
        self.policy(fixed('100'))
        before = self.store.markup_policy().to_data()
        version = self.store.get('pricing_version')
        self.store.db.execute("""CREATE TRIGGER fail_pricing_version
            BEFORE INSERT ON settings WHEN NEW.key='pricing_version'
            BEGIN SELECT RAISE(ABORT, 'test-policy-failure'); END""")
        self.store.db.execute("""CREATE TRIGGER fail_pricing_version_update
            BEFORE UPDATE ON settings WHEN NEW.key='pricing_version'
            BEGIN SELECT RAISE(ABORT, 'test-policy-failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.set_markup_policy(MarkupPolicy(default=fixed('2500')))
        self.assertEqual(self.store.markup_policy().to_data(), before)
        self.assertEqual(self.store.get('pricing_version'), version)
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertIn('85 100 ₽', self.sent[-1][2])
