"""Regression cases from the selected test group's unanswered requests.

The supplier excerpt contains only positions actually present in the user's
price message. It deliberately has no base S25 Black: Ultra/Edge Black must
never become an answer to that request.
"""
import tempfile
import unittest
from pathlib import Path

from catalog_bot.engine import Engine, Incoming
from catalog_bot.prices import parse_price_feed
from catalog_bot.storage import Store


SUPPLIER_EXCERPT = """Цены на Все товары

• Galaxy S25 12/256 Navy SM-S931B🇮🇳
🇮🇳 От 1 шт - 50 800

• Galaxy S25 12/256 Silver Shadow SM-S931B/DS🇮🇳
🇮🇳 От 1 шт - 50 800

• Galaxy S25 12/256 Icyblue SM-S931B/DS🇮🇳
🇮🇳 От 1 шт - 49 800

• Galaxy S25 FE 8/256 Navy SM-S731B/DS🇦🇪
🇦🇪 От 1 шт - 42 800

• Galaxy S25 Edge 12/256 Jetblack SM-S937B/DS🇪🇺
🇪🇺 От 1 шт - 49 000

• Galaxy S25 Ultra 12/256 Black SM-S938B/DS🇦🇪
🇦🇪 От 1 шт - 66 500

• Xiaomi 17T 12/512 Black🇪🇺
🇪🇺 От 1 шт - 44 700

• iPhone 16 Pro Max 512 Black🇨🇳
🇨🇳 От 1 шт - 110 000

Всего найдено 8 товаров
"""


class ScreenshotRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'bot.sqlite3')
        self.store.replace_products(list(parse_price_feed(
            SUPPLIER_EXCERPT, require_complete=True).products))
        self.store.toggle_chat(-123, 'Тест')
        self.store.set('active', True)
        self.store.set('price_ready', True)
        self.sent = []

        async def send(recipient, source_message_id, text):
            self.sent.append((recipient, source_message_id, text))

        self.engine = Engine(self.store, send, clock=lambda: 1000)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, text, **changes):
        data = dict(chat_id=-123, message_id=1, sender_id=55,
                    text=text, timestamp=1000)
        return Incoming(**(data | changes))

    async def test_incoming_short_navy_query_privately_quotes_its_author(self):
        result = await self.engine.process(self.message('s25 navy', message_id=28))
        self.assertEqual(result, 'sent')
        self.assertEqual(len(self.sent), 1)
        recipient, source_message_id, reply = self.sent[0]
        self.assertEqual((recipient, source_message_id), (55, 28))
        self.assertEqual(self.store.delivery(-123, 28)['status'], 'sent')
        self.assertIsNone(self.store.delivery(55, 28))
        self.assertIn('Galaxy S25 12/256 Navy', reply)
        self.assertNotIn('SM-S931B', reply)
        self.assertIn('50 800 ₽', reply)
        self.assertEqual(reply.count('• '), 1)
        for incorrect in ('Ultra', 'Edge', ' FE ', 'Xiaomi', 'iPhone'):
            self.assertNotIn(incorrect, reply)

    async def test_other_screenshot_messages_do_not_block_incoming_navy(self):
        for message_id, text in enumerate(
                ('S25 256 black', 'Samsung', 'Samsung', 'Iphone'), start=1):
            result = await self.engine.process(self.message(
                text, message_id=message_id, outgoing=True))
            self.assertEqual(result, 'ignored')
        self.assertEqual(await self.engine.process(
            self.message('s25 navy', message_id=28)), 'sent')
        self.assertEqual(len(self.sent), 1)

    async def test_missing_base_s25_black_is_not_replaced_by_another_version(self):
        for text in ('S25 256 black', 'сасунг с25 256 черный'):
            with self.subTest(text=text):
                self.assertEqual(await self.engine.process(self.message(text)), 'no_match')
        self.assertEqual(self.sent, [])

    async def test_brand_only_returns_the_matching_products_in_full(self):
        self.assertEqual(await self.engine.process(self.message('Samsung', message_id=40)), 'sent')
        self.assertIn('Galaxy S25', self.sent[-1][2])
        self.assertNotIn('iPhone', self.sent[-1][2])
        self.assertNotIn('Xiaomi', self.sent[-1][2])
        self.assertEqual(await self.engine.process(self.message('Iphone', message_id=41)), 'sent')
        self.assertIn('iPhone 16 Pro Max 512 Black', self.sent[-1][2])
        self.assertNotIn('Galaxy', self.sent[-1][2])
        self.assertNotIn('Xiaomi', self.sent[-1][2])

    async def test_cyrillic_samsung_typo_quotes_same_base_navy_position(self):
        result = await self.engine.process(self.message('сасунг с25 256 темно-синий'))
        self.assertEqual(result, 'sent')
        reply = self.sent[0][2]
        self.assertIn('Galaxy S25 12/256 Navy', reply)
        self.assertNotIn('SM-S931B', reply)
        self.assertIn('50 800 ₽', reply)
        self.assertEqual(reply.count('• '), 1)


if __name__ == '__main__':
    unittest.main()
