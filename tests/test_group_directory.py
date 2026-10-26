import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from telethon import errors
from telethon.requestiter import RequestIter

from catalog_bot.app import Application, Config
from catalog_bot.group_directory import GroupDirectory
from catalog_bot.storage import Store
from tests import test_app as helpers


def group(number, *, is_group=True, title=None):
    return SimpleNamespace(id=-1000-number, title=title or f'Группа {number}', is_group=is_group)


class ControlledSleep:
    def __init__(self):
        self.now = 1000
        self.calls = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    def clock(self): return self.now

    async def sleep(self, seconds):
        self.calls.append(seconds)
        self.entered.set()
        await self.release.wait()
        self.release.clear()
        self.now += seconds


class PageIterator(RequestIter):
    """Use Telethon's real iterator/cursor machinery without Telegram calls."""
    async def _init(self):
        self.page = 0
        self.client.initializations += 1

    async def _load_next_chunk(self):
        self.client.offsets.append(self.page)
        if self.client.failure is not None and self.page == self.client.fail_at:
            error, self.client.failure = self.client.failure, None
            raise error
        if self.page >= len(self.client.pages):
            return True
        self.buffer.extend(self.client.pages[self.page])
        self.page += 1
        return self.page == len(self.client.pages)


class DirectoryClient:
    def __init__(self, pages=None):
        self.pages = pages if pages is not None else [[group(1)], [group(2)]]
        self.factories, self.offsets = [], []
        self.initializations = 0
        self.failure, self.fail_at = None, 1

    def is_connected(self): return True

    def iter_dialogs(self, **kwargs):
        self.factories.append(kwargs)
        return PageIterator(self, None)


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'catalog.sqlite3'
        self.store = Store(self.path)
        self.store.set('telegram_account_id', 77)
        self.available = asyncio.Event()
        self.available.set()
        self.client, self.wait = DirectoryClient(), ControlledSleep()
        self.directory = self.make_directory()

    def make_directory(self):
        return GroupDirectory(self.store, self.client, self.available, AsyncMock(),
                              clock=self.wait.clock, sleep=self.wait.sleep, page_pause=0)

    async def asyncTearDown(self):
        task = self.directory.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    async def pause_on_flood(self):
        self.client.failure = errors.FloodWaitError(None, capture=26)
        task = self.directory.ensure_started()
        await asyncio.wait_for(self.wait.entered.wait(), 3)
        return task

    async def test_flood_preserves_partial_groups_and_resumes_same_telethon_page(self):
        task = await self.pause_on_flood()
        self.assertEqual(list(self.directory.items), [-1001])
        self.assertEqual(self.client.offsets, [0, 1])
        self.assertFalse(self.directory.complete)
        self.assertEqual(self.wait.calls, [26])
        for _ in range(10):
            self.assertIs(self.directory.ensure_started(refresh=True), task)
        self.assertEqual(self.client.offsets, [0, 1])
        self.wait.release.set()
        await asyncio.wait_for(task, 3)
        self.assertEqual(self.client.offsets, [0, 1, 1])
        self.assertEqual(self.client.initializations, 1)
        self.assertEqual(list(self.directory.items), [-1001, -1002])
        self.assertTrue(self.directory.complete)

    async def test_cached_browsing_and_rapid_refresh_do_not_reenumerate(self):
        await self.directory.ensure_started()
        self.assertIsNone(self.directory.ensure_started())
        self.assertIsNone(self.directory.ensure_started(refresh=True))
        self.wait.now += 61
        self.assertIsNone(self.directory.ensure_started())
        await self.directory.ensure_started(refresh=True)
        self.assertEqual(len(self.client.factories), 2)

    async def test_complete_cache_survives_reopen_and_is_scoped_to_account(self):
        await self.directory.ensure_started()
        self.store.close()
        self.store = Store(self.path)
        self.directory = self.make_directory()
        self.assertEqual(len(self.directory.items), 2)
        self.assertIsNone(self.directory.ensure_started())
        self.directory.bind(88)
        self.assertEqual(self.directory.items, {})
        self.assertFalse(self.directory.complete)

    async def test_partial_cache_and_flood_deadline_survive_restart(self):
        task = await self.pause_on_flood()
        self.directory.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.store.close()
        self.store = Store(self.path)
        self.wait.entered.clear()
        self.directory = self.make_directory()
        self.assertEqual(list(self.directory.items), [-1001])
        old_calls = len(self.client.offsets)
        task = self.directory.ensure_started()
        await asyncio.wait_for(self.wait.entered.wait(), 3)
        self.assertEqual(len(self.client.offsets), old_calls)
        self.wait.release.set()
        await asyncio.wait_for(task, 3)
        self.assertTrue(self.directory.complete)

    async def test_failed_refresh_retains_cache_until_a_complete_new_scan(self):
        await self.directory.ensure_started()
        self.wait.now += 61
        self.client.pages = [[group(3)], [group(4)]]
        self.client.failure = errors.RPCError(None, 'synthetic failure', 400)
        with self.assertLogs('catalog_bot.group_directory', level='WARNING'):
            await self.directory.ensure_started(refresh=True)
        self.assertEqual(set(self.directory.items), {-1001, -1002, -1003})
        self.assertFalse(self.directory.complete)
        self.assertIn('RPCError', self.directory.error)
        self.wait.now += 31
        await self.directory.ensure_started(refresh=True)
        self.assertEqual(set(self.directory.items), {-1003, -1004})
        self.assertTrue(self.directory.complete)

    async def test_disconnected_account_does_not_query_telegram(self):
        self.available.clear()
        task = self.directory.ensure_started()
        await asyncio.sleep(.02)
        self.assertEqual(self.client.offsets, [])
        self.available.set()
        await asyncio.wait_for(task, 3)
        self.assertTrue(self.directory.complete)

    async def test_private_dialogs_and_channels_are_filtered_archived_are_included(self):
        self.client.pages = [[group(1), group(2, is_group=False), group(3)]]
        await self.directory.ensure_started()
        self.assertEqual(list(self.directory.items), [-1001, -1003])
        self.assertEqual(self.client.factories, [{'ignore_migrated': True}])

    async def test_switching_account_cancels_old_scan_and_drops_old_groups(self):
        task = await self.pause_on_flood()
        self.directory.bind(88)
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.directory.items, {})
        self.client.pages = [[group(88)]]
        await self.directory.ensure_started()
        self.assertEqual(list(self.directory.items), [-1088])
        self.assertEqual(self.store.get('group_directory')['account_id'], 88)


class GroupPickerTests(unittest.IsolatedAsyncioTestCase):
    feed = helpers.ControllerTests.feed
    texts = helpers.ControllerTests.texts

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'catalog.sqlite3')
        self.store.set('telegram_account_id', 77)
        self.transport = helpers.LocalSession()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.transport)
        self.client, self.wait = DirectoryClient(), ControlledSleep()
        self.app = Application(Config(123, 'test', '', 55, Path(self.temp.name)), self.store, self.client, self.bot)
        self.app.auth.ready = True
        self.app.account_available.set()
        self.directory = self.app.group_directory
        self.directory.clock, self.directory.sleep, self.directory.page_pause = self.wait.clock, self.wait.sleep, 0
        self.update_id = 1000

    async def asyncTearDown(self):
        task = self.directory.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await self.bot.session.close()
        self.store.close()
        self.temp.cleanup()

    async def callback(self, data, *, root=False, user_id=55, chat_type='private'):
        self.update_id += 1
        panel = self.app.chat_panel
        message_id = 900 if root or panel is None else panel.message_id
        message = Message(message_id=message_id, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type=chat_type))
        callback = CallbackQuery(id=str(self.update_id), from_user=User(id=user_id, is_bot=False, first_name='Test'),
                                 chat_instance='test', message=message, data=data)
        await self.app.dp.feed_update(self.bot, Update(update_id=self.update_id, callback_query=callback))

    async def finish(self):
        if self.directory.task:
            await asyncio.wait_for(asyncio.shield(self.directory.task), 3)

    async def test_found_groups_are_selectable_during_flood_and_list_finishes_automatically(self):
        self.client.failure = errors.FloodWaitError(None, capture=26)
        await self.feed('/chats')
        await asyncio.wait_for(self.wait.entered.wait(), 3)
        self.assertIn('26 сек', self.texts()[-1])
        self.assertIn('Группы: 1', self.texts()[-1])
        await self.callback('toggle:0:-1001')
        self.assertEqual(self.store.chats(), {-1001: 'Группа 1'})
        self.assertEqual(self.client.offsets, [0, 1])
        self.wait.release.set()
        await self.finish()
        self.assertIn('Группы: 2', self.texts()[-1])
        self.assertNotIn('Продолжу загрузку', self.texts()[-1])
        self.assertTrue(any(c.__api_method__ == 'editMessageText' for c in self.transport.calls))

    async def test_repeated_commands_paging_and_toggle_use_cached_list(self):
        self.client.pages = [[group(i) for i in range(12)]]
        await self.feed('/chats')
        await self.finish()
        await self.feed('/chats')
        await self.callback('chats:1')
        self.assertIn('Страница 2', self.texts()[-1])
        await self.callback('toggle:1:-1009')
        self.assertIn(-1009, self.store.chats())
        self.assertEqual(len(self.client.factories), 1)

    async def test_repeated_button_clicks_share_one_panel_and_one_scan(self):
        self.client.failure = errors.FloodWaitError(None, capture=26)
        await asyncio.gather(self.callback('chats:0', root=True), self.callback('chats:0', root=True))
        await asyncio.wait_for(self.wait.entered.wait(), 3)
        self.assertEqual(len(self.client.factories), 1)
        self.assertEqual(sum(c.__api_method__ == 'sendMessage' for c in self.transport.calls), 1)

    async def test_failed_first_load_is_not_reported_as_no_groups(self):
        self.client.fail_at = 0
        self.client.failure = errors.RPCError(None, 'synthetic failure', 400)
        with self.assertLogs('catalog_bot.group_directory', level='WARNING'):
            await self.feed('/chats')
            await self.finish()
        self.assertIn('Не удалось закончить', self.texts()[-1])
        self.assertNotIn('пока не состоит', self.texts()[-1])

    async def test_only_complete_empty_scan_reports_that_account_has_no_groups(self):
        self.client.pages = [[]]
        await self.feed('/chats')
        await self.finish()
        self.assertIn('пока не состоит', self.texts()[-1])

    async def test_private_owner_guard_prevents_loading_or_selecting_groups(self):
        await self.feed('/chats', user_id=66)
        await self.feed('/chats', chat_type='group')
        await self.callback('chats:0', user_id=66)
        await self.callback('chats_search', user_id=66)
        await self.feed('/chats название', user_id=66)
        self.assertEqual(self.client.factories, [])
        self.assertEqual(self.store.chats(), {})

    def visible_buttons(self):
        markup = next(call.reply_markup for call in reversed(self.transport.calls)
                      if getattr(call, 'reply_markup', None) is not None)
        return [button for row in markup.inline_keyboard for button in row]

    async def test_search_button_accepts_partial_name_ignoring_case_and_yo(self):
        self.client.pages = [[group(1, title='Зелёный | ОПТ'), group(2, title='Apple'),
                              group(3, title='Зеленый закупки')]]
        await self.callback('chats_search', root=True)
        self.assertIn('часть названия', self.texts()[-1])
        self.assertEqual(self.client.factories, [])
        await self.feed('  ЗЕЛЕНЫЙ   опт  ')
        await self.finish()
        self.assertIn('Найдено: 1 из 3', self.texts()[-1])
        choices = [b.callback_data for b in self.visible_buttons() if b.callback_data.startswith('toggle:')]
        self.assertEqual(choices, ['toggle:0:-1001'])

    async def test_filtered_paging_and_selection_keep_query_and_exact_group_id(self):
        self.client.pages = [[group(i, title=f'Опт APPLE Москва {i}') for i in range(10)] +
                             [group(i, title=f'Samsung {i}') for i in range(10, 14)]]
        await self.feed('/chats apple опт')
        await self.finish()
        self.assertIn('Найдено: 10 из 14', self.texts()[-1])
        await self.callback('chats:1')
        self.assertIn('Страница 2', self.texts()[-1])
        choices = [b.callback_data for b in self.visible_buttons() if b.callback_data.startswith('toggle:')]
        self.assertEqual(choices, ['toggle:1:-1008', 'toggle:1:-1009'])
        await self.callback('toggle:1:-1009')
        self.assertEqual(self.store.chats(), {-1009: 'Опт APPLE Москва 9'})
        self.assertEqual(self.app.chat_query, 'apple опт')
        self.assertEqual(len(self.client.factories), 1)

    async def test_all_groups_and_plain_command_clear_filter_preserving_selection(self):
        self.client.pages = [[group(1, title='Apple'), group(2, title='Samsung')]]
        await self.feed('/chats apple')
        await self.finish()
        await self.callback('toggle:0:-1001')
        await self.callback('chats_all')
        self.assertEqual(self.app.chat_query, '')
        self.assertIn('Группы: 2', self.texts()[-1])
        await self.feed('/chats samsung')
        self.assertEqual(self.app.chat_query, 'samsung')
        await self.feed('/chats')
        self.assertEqual(self.app.chat_query, '')
        self.assertEqual(self.store.chats(), {-1001: 'Apple'})
        self.assertEqual(len(self.client.factories), 1)

    async def test_search_updates_when_matching_group_arrives_after_flood(self):
        self.client.pages = [[group(1, title='Обсуждения')], [group(2, title='Техника опт')]]
        self.client.failure = errors.FloodWaitError(None, capture=26)
        await self.feed('/chats опт')
        await asyncio.wait_for(self.wait.entered.wait(), 3)
        self.assertIn('Пока совпадений нет', self.texts()[-1])
        self.assertNotIn('пока не состоит', self.texts()[-1])
        self.wait.release.set()
        await self.finish()
        self.assertIn('Найдено: 1 из 2', self.texts()[-1])
        self.assertEqual(self.app.chat_query, 'опт')
        self.assertIn('toggle:0:-1002', [b.callback_data for b in self.visible_buttons()])

    async def test_empty_search_results_offer_reset_instead_of_no_groups_claim(self):
        await self.feed('/chats отсутствует')
        await self.finish()
        self.assertIn('совпадений нет', self.texts()[-1])
        self.assertNotIn('пока не состоит', self.texts()[-1])
        self.assertIn('chats_all', [b.callback_data for b in self.visible_buttons()])

    async def test_invalid_search_keeps_input_mode_and_does_not_query_telegram(self):
        await self.callback('chats_search', root=True)
        await self.feed('я' * 201)
        self.assertIn('200 символов', self.texts()[-1])
        self.assertEqual(self.client.factories, [])
        await self.feed('Группа 2')
        await self.finish()
        self.assertIn('Найдено: 1 из 2', self.texts()[-1])
