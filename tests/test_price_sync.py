import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import ButtonTypeDefault, InlineButtonTypeUrl

from catalog_bot.price_sync import PriceSync
from catalog_bot.prices import parse_price_feed
from catalog_bot.storage import Store


PRICE = '• iPhone 17 256GB Black🇨🇳\n🇨🇳 От 1 шт - 85 000\nВсего найдено 1 товаров'


class SourceClient:
    def __init__(self):
        self.bot = True
        self.calls = []
        self.response = PRICE
        self.message = SimpleNamespace(id=10, buttons=[])
        self.sync = None

    async def get_entity(self, name):
        return SimpleNamespace(id=999, bot=self.bot)

    async def get_messages(self, peer, limit):
        return [self.message]

    async def emit(self, message_id=11):
        await self.sync.receive(SimpleNamespace(chat_id=999, id=message_id, out=False, raw_text=self.response))

    async def send_message(self, peer, text, **kwargs):
        self.calls.append(text)
        await self.emit()


class SourceSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'bot.sqlite3')
        self.client = SourceClient()
        self.notifications = []

        async def notify(text):
            self.notifications.append(text)

        self.sync = PriceSync(self.store, self.client, '@sourcebot', notify, request_text='/price', timeout=.02)
        self.client.sync = self.sync

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_command_refresh_commits_complete_price_and_changes(self):
        self.assertTrue(await self.sync.refresh())
        self.assertEqual(self.client.calls, ['/price'])
        self.assertTrue(self.store.get('price_ready'))
        self.client.response = PRICE.replace('85 000', '86 500')
        self.assertTrue(await self.sync.refresh())
        self.assertEqual(str(self.store.products()[0].price), '86500')
        self.assertIn('Изменились цены: 1', self.notifications[-1])

    async def test_partial_response_preserves_old_price_but_disables_quoting(self):
        self.store.apply_price_snapshot(parse_price_feed(PRICE), 'bot:999')
        self.client.response = PRICE.replace('85 000', '86 000').replace('1 товаров', '425 товаров')
        self.assertFalse(await self.sync.refresh())
        self.assertEqual(str(self.store.products()[0].price), '85000')
        self.assertFalse(self.store.get('price_ready'))

    async def test_unchanged_poll_does_not_send_repeated_owner_alert(self):
        self.assertTrue(await self.sync.refresh())
        count = len(self.notifications)
        self.assertTrue(await self.sync.refresh())
        self.assertEqual(len(self.notifications), count)

    async def test_resolved_source_must_be_a_bot(self):
        self.client.bot = False
        self.assertFalse(await self.sync.refresh())
        self.assertEqual(self.client.calls, [])

    async def test_button_can_return_an_edited_message(self):
        self.sync.request_text, self.sync.button_text = '', 'Все товары'
        clicked = []

        async def click(**kwargs):
            clicked.append(kwargs)
            await self.client.emit(10)

        self.client.message.buttons = [[SimpleNamespace(text='Все товары', button=SimpleNamespace(type=ButtonTypeDefault()))]]
        self.client.message.click = click
        self.assertTrue(await self.sync.refresh())
        self.assertEqual(clicked, [{'text': 'Все товары'}])
        self.assertEqual(self.client.calls, [])

    async def test_url_button_is_not_used_as_a_price_action(self):
        self.sync.request_text, self.sync.button_text = '', 'Все товары'
        self.client.message.buttons = [[SimpleNamespace(text='Все товары', button=SimpleNamespace(type=InlineButtonTypeUrl('https://example.com')))]]
        self.assertFalse(await self.sync.refresh())
        self.assertEqual(self.client.calls, [])

    async def test_messages_from_other_bot_are_not_imported(self):
        await self.sync.resolve()
        self.sync.collector.begin(10)
        await self.sync.receive(SimpleNamespace(chat_id=1000, id=11, out=False, raw_text=PRICE))
        self.assertEqual(self.store.products(), [])


if __name__ == '__main__':
    unittest.main()
