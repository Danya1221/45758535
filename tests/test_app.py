import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
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
        if method.__api_method__ in {'sendMessage', 'sendPhoto', 'editMessageText'}:
            return Message(message_id=getattr(method, 'message_id', None) or len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type='private'), text=getattr(method, 'text', None)).as_(bot)
        return True

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''


class LocalClient:
    def is_connected(self):
        return True

    async def set_receive_updates(self, enabled):
        self.receive_updates = enabled

    async def iter_dialogs(self, **kwargs):
        yield SimpleNamespace(id=-100123, title='Закупки', is_group=True)
        yield SimpleNamespace(id=1, title='Личка', is_group=False)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'bot.sqlite3')
        self.transport = LocalSession()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.transport)
        self.app = Application(Config(123, 'test', '', 55, Path(self.temp.name)), self.store, LocalClient(), self.bot)
        self.app.auth.ready = True
        self.app.account_available.set()
        self.update_id = 0

    async def asyncTearDown(self):
        task = self.app.group_directory.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await self.bot.session.close()
        self.store.close()
        self.temp.cleanup()

    async def feed(self, text, user_id=55, chat_type='private'):
        self.update_id += 1
        message = Message(message_id=self.update_id, date=datetime.now(timezone.utc),
                          chat=Chat(id=user_id, type=chat_type), from_user=User(id=user_id, is_bot=False, first_name='Test'), text=text)
        await self.app.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message))

    def texts(self):
        return [m.text for m in self.transport.calls if m.__api_method__ in {'sendMessage', 'editMessageText'}]

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
        if self.app.group_directory.task:
            await self.app.group_directory.task
        self.assertIn('Группы: 1', self.texts()[-1])
        message = Message(message_id=9, date=datetime.now(timezone.utc), chat=Chat(id=55, type='private'))
        callback = CallbackQuery(id='123', from_user=User(id=55, is_bot=False, first_name='Test'),
                                 chat_instance='instance', message=message, data='toggle:0:-100123')
        await self.app.dp.feed_update(self.bot, Update(update_id=100, callback_query=callback))
        self.assertEqual(self.store.chats(), {-100123: 'Закупки'})

    async def test_supplier_price_chunks_and_price_changes(self):
        await self.feed('/uploadprice')
        await self.feed('**• iPhone 17 256GB Black🇨🇳**\n🇨🇳 От 1 шт - 85 000')
        self.assertEqual(self.store.products(), [])
        await self.feed('Всего найдено 1 товаров')
        self.assertEqual(str(self.store.products()[0].price), '85000')
        self.assertIsNone(self.store.products()[0].stock)
        await self.feed('/uploadprice')
        await self.feed('**• iPhone 17 256GB Black🇨🇳**\n🇨🇳 От 1 шт - 86 000\nВсего найдено 1 товаров')
        await self.feed('/changes')
        self.assertIn('85000 → 86000', self.texts()[-1])
        await self.feed('/test Куплю iPhone 17 256 Black')
        self.assertIn('По актуальному прайсу:', self.texts()[-1])
        self.assertIn('86 000 ₽', self.texts()[-1])

    async def test_incomplete_price_leaves_previous_version(self):
        await self.feed('/catalog')
        await self.feed('iPhone 17|85000|2')
        await self.feed('/uploadprice')
        await self.feed('• iPhone 18\nОт 1 шт - 90 000\nВсего найдено 425 товаров')
        self.assertEqual(self.store.products()[0].name, 'iPhone 17')
        self.assertIn('Прайс не изменён', self.texts()[-1])

    async def test_test_command_explains_strict_matching_without_sending(self):
        await self.feed('/catalog')
        await self.feed('Note 15 8/256 Blue (NO NFC)|18000|1')
        await self.feed('/test Куплю Note 15 NFC')
        self.assertIn('Совпадений: 0', self.texts()[-1])
        self.assertIn('Отсеяно: NFC — 1', self.texts()[-1])
        self.assertIn('Ответа не будет', self.texts()[-1])
        self.assertFalse(self.store.get('active', False))
        await self.feed('/catalog')
        await self.feed('iPhone 17 256 Black SIM|85000|1')
        await self.feed('/test Куплю iPhone 17 256 Black SIM')
        self.assertIn('модель: iPhone 17', self.texts()[-1])
        self.assertIn('память: 256 ГБ', self.texts()[-1])
        self.assertIn('85 000 ₽', self.texts()[-1])

    async def test_markup_changes_quote_and_budget_but_preserves_source_export(self):
        await self.feed('/catalog')
        await self.feed('Samsung Galaxy S26 Ultra 12/512 Black|91500|1')
        await self.feed('/markup phones percent 2')
        await self.feed('/test Куплю сасунг с26 ультра 512 черный до 93 000')
        self.assertIn('Совпадений: 0', self.texts()[-1])
        await self.feed('/test Куплю сасунг с26 ультра 512 черный до 93 330')
        self.assertIn('93 330 ₽', self.texts()[-1])
        self.assertIn('Категории для наценки: Телефоны', self.texts()[-1])
        self.assertEqual(self.store.products()[0].price, Decimal('91500'))
        await self.feed('/stock')
        document = self.transport.calls[-1].document
        exported = document.data.decode('utf-8-sig')
        self.assertIn('|91500|', exported)
        self.assertNotIn('93330', exported)

    async def test_markup_category_off_clear_and_reset(self):
        await self.feed('/catalog')
        await self.feed('iPhone 17 256 Black|85000|1\nXiaomi Pad 7 8/256 Gray|28900|1')
        await self.feed('/markup default fixed 1000')
        await self.feed('/markup phones percent 2')
        await self.feed('/test iPhone 17 256 Black')
        self.assertIn('86 700 ₽', self.texts()[-1])
        await self.feed('/test Xiaomi Pad 7 8/256 Gray')
        self.assertIn('29 900 ₽', self.texts()[-1])
        await self.feed('/markup phones off')
        await self.feed('/test iPhone 17')
        self.assertIn('85 000 ₽', self.texts()[-1])
        await self.feed('/markup phones clear')
        await self.feed('/test iPhone 17')
        self.assertIn('86 000 ₽', self.texts()[-1])
        await self.feed('/markup reset')
        await self.feed('/test iPhone 17')
        self.assertIn('85 000 ₽', self.texts()[-1])

    async def test_invalid_markup_and_unauthorized_commands_do_not_change_rules(self):
        await self.feed('/markup phones fixed 1500')
        before = self.store.markup_policy().to_data()
        version = self.store.get('pricing_version')
        for text in ('/markup phones fixed -10', '/markup phones percent NaN',
                     '/markup unknown fixed 50', '/markup phones fixed 10 extra'):
            await self.feed(text)
            self.assertIn('Наценка не изменена', self.texts()[-1])
        await self.feed('/markup reset', user_id=66)
        await self.feed('/markup reset', chat_type='group')
        self.assertEqual(self.store.markup_policy().to_data(), before)
        self.assertEqual(self.store.get('pricing_version'), version)

    async def test_markup_leaves_import_mode_and_menu_button_shows_help(self):
        await self.feed('/catalog')
        message = Message(message_id=9, date=datetime.now(timezone.utc), chat=Chat(id=55, type='private'))
        callback = CallbackQuery(id='markup-test', from_user=User(id=55, is_bot=False, first_name='Test'),
                                 chat_instance='instance', message=message, data='markup')
        await self.app.dp.feed_update(self.bot, Update(update_id=100, callback_query=callback))
        self.assertIn('/markup phones percent 2', self.texts()[-1])
        self.assertLess(len(self.texts()[-1]), 4000)
        await self.feed('iPhone 17|85000|1')
        self.assertEqual(self.store.products(), [])


if __name__ == '__main__':
    unittest.main()
