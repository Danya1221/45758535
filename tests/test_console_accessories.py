import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from catalog_bot.catalog import Product, find_products
from catalog_bot.engine import Engine, Incoming
from catalog_bot.storage import Store


CONSOLE = Product('Nintendo Switch 2', Decimal('47000'), None, country='🇬🇧')
JOYCON = Product('Nintendo Switch Joy-Con 2 Controller', Decimal('4600'), None, country='🇰🇷')
PRO = Product('Nintendo Switch 2 Pro Controller', Decimal('6000'), None)
BUNDLE = Product('Nintendo Switch 2 + Joy-Con Controller Bundle', Decimal('51000'), None)
CONTROLLER_BUNDLE = Product('Nintendo Switch Joy-Con 2 Controller Bundle', Decimal('8000'), None)


class ConsoleAccessoryTests(unittest.TestCase):
    products = [CONSOLE, JOYCON, PRO, BUNDLE, CONTROLLER_BUNDLE]

    def test_console_requests_exclude_individual_controllers(self):
        for query in ('Nintendo Switch 2', 'куплю Nintendo Switch 2', 'Nintendo'):
            with self.subTest(query=query):
                self.assertEqual(find_products(query, self.products)[1], [CONSOLE, BUNDLE])

    def test_controller_requests_do_not_return_the_console(self):
        for query in ('joy con 2', 'Joy-Con 2', 'джой кон 2', 'Nintendo Switch Joy-Con 2 Controller',
                      'геймпад Nintendo Switch 2'):
            with self.subTest(query=query):
                self.assertEqual(find_products(query, self.products)[1], [JOYCON, CONTROLLER_BUNDLE])
        self.assertEqual(find_products('Nintendo Switch 2 Pro Controller', self.products)[1], [PRO])

    def test_console_request_cannot_be_answered_only_with_controller(self):
        self.assertEqual(find_products('Nintendo Switch 2', [JOYCON])[1], [])


class ConsoleResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_console_reaches_customer_and_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db.sqlite3')
            try:
                store.replace_products([CONSOLE, JOYCON])
                store.set('active', True)
                store.set('telegram_account_id', 77)
                store.toggle_chat(-100, 'test')
                sent = []

                async def send(buyer, source, text):
                    sent.append(text)
                    return [901]

                engine = Engine(store, send, clock=lambda: 1000)
                self.assertEqual(await engine.process(Incoming(-100, 1, 55, 'Nintendo Switch 2', 1000)), 'sent')
                self.assertIn('Nintendo Switch 2 🇬🇧 — 47 000 ₽', sent[0])
                self.assertNotIn('Joy-Con', sent[0])
                quote = store.quote_for_reply(77, 55, 901, now=1000)
                self.assertEqual(quote['products'], [CONSOLE])
            finally:
                store.close()
