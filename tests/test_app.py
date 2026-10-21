import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from catalog_bot.app import Application, Config
from catalog_bot.storage import Store


class LocalSession(BaseSession):
    """Telegram transport stub: no network calls, real aiogram routing."""
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__api_method__ == 'sendMessage':
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type='private'), text=method.text).as_(bot)
        return True

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''


class LocalClient:
    def is_connected(self):
        return True

    async def iter_dialogs(self):
        yield SimpleNamespace(id=-100123, title='Закупки', is_group=True)
        yield SimpleNamespace(id=1, title='Личка', is_group=False)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'bot.sqlite3')
        self.transport = LocalSession()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.transport)
        self.app = Application(Config(123, 'test', '', 55, Path(self.temp.name)), self.store, LocalClient(), self.bot)
        self.update_id = 0

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.store.close()
        self.temp.cleanup()

    async def feed(self, text, user_id=55, chat_type='private'):
        self.update_id += 1
        message = Message(message_id=self.update_id, date=datetime.now(timezone.utc),
                          chat=Chat(id=user_id, type=chat_type), from_user=User(id=user_id, is_bot=False, first_name='Test'), text=text)
        await self.app.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message))

    def texts(self):
        return [m.text for m in self.transport.calls if m.__api_method__ == 'sendMessage']

    async def test_only_owner_private_messages_control_bot(self):
        await self.feed('/start', user_id=66)
        await self.feed('/start', chat_type='group')
        self.assertEqual(self.transport.calls, [])
        await self.feed('/start')
        self.assertIn('Бот отвечает', self.texts()[-1])

    async def test_import_test_and_run_flow(self):
        await self.feed('/catalog')
        await self.feed('iPhone 17 256GB Black SIM|85000|2')
        self.assertEqual(len(self.store.products()), 1)
        await self.feed('/test нужен iPhone 17')
        self.assertIn('85 000 ₽', self.texts()[-1])
        await self.feed('/run')
        self.assertFalse(self.store.get('active'))
        self.store.toggle_chat(-100123, 'Закупки')
        await self.feed('/run')
        self.assertTrue(self.store.get('active'))
        await self.feed('/pause')
        self.assertFalse(self.store.get('active'))

    async def test_invalid_import_keeps_old_catalog_and_waits_for_correction(self):
        await self.feed('/catalog')
        await self.feed('iPhone 17|85000|2')
        await self.feed('/catalog')
        await self.feed('iPhone 18|nan|1')
        self.assertEqual(self.store.products()[0].name, 'iPhone 17')
        await self.feed('iPhone 18|90000|1')
        self.assertEqual(self.store.products()[0].name, 'iPhone 18')

    async def test_chat_picker_filters_private_dialogs_and_toggles(self):
        await self.feed('/chats')
        self.assertIn('Группы: 1', self.texts()[-1])
        message = Message(message_id=9, date=datetime.now(timezone.utc), chat=Chat(id=55, type='private'))
        callback = CallbackQuery(id='123', from_user=User(id=55, is_bot=False, first_name='Test'),
                                 chat_instance='instance', message=message, data='toggle:0:-100123')
        await self.app.dp.feed_update(self.bot, Update(update_id=100, callback_query=callback))
        self.assertEqual(self.store.chats(), {-100123: 'Закупки'})

    async def test_supplier_price_chunks_and_price_changes(self):
        await self.feed('/price')
        await self.feed('**• iPhone 17 256GB Black🇨🇳**\n🇨🇳 От 1 шт - 85 000')
        self.assertEqual(self.store.products(), [])
        await self.feed('Всего найдено 1 товаров')
        self.assertEqual(str(self.store.products()[0].price), '85000')
        self.assertIsNone(self.store.products()[0].stock)
        await self.feed('/price')
        await self.feed('**• iPhone 17 256GB Black🇨🇳**\n🇨🇳 От 1 шт - 86 000\nВсего найдено 1 товаров')
        await self.feed('/changes')
        self.assertIn('85000 → 86000', self.texts()[-1])
        await self.feed('/test Куплю iPhone 17 256 Black')
        self.assertIn('По актуальному прайсу:', self.texts()[-1])
        self.assertIn('86 000 ₽', self.texts()[-1])

    async def test_incomplete_price_leaves_previous_version(self):
        await self.feed('/catalog')
        await self.feed('iPhone 17|85000|2')
        await self.feed('/price')
        await self.feed('• iPhone 18\nОт 1 шт - 90 000\nВсего найдено 425 товаров')
        self.assertEqual(self.store.products()[0].name, 'iPhone 17')
        self.assertIn('Прайс не изменён', self.texts()[-1])


if __name__ == '__main__':
    unittest.main()
