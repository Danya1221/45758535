import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import DeleteMessages, SendMessage
from aiogram.types import Chat, Message, Update, User

from catalog_bot.chat_cleanup import OwnerChatCleanup, OwnerIncomingMiddleware, OwnerOutgoingMiddleware
from catalog_bot.storage import Store


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'store.sqlite3'
        self.store = Store(self.path)
        self.job = OwnerChatCleanup(self.store, 55, 123)
        self.bot = SimpleNamespace(delete_messages=AsyncMock(return_value=True))
        self.menu = AsyncMock()
        self.before = datetime(2026, 11, 2, 20, 59, tzinfo=timezone.utc)  # 23:59 MSK
        self.after = self.before + timedelta(minutes=2)  # next day, 00:01 MSK
        self.job.initialize(self.before)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def message(self, message_id, *, chat_id=55, chat_type='private', sender_id=55, date=None):
        return Message(message_id=message_id, chat=Chat(id=chat_id, type=chat_type),
                       from_user=User(id=sender_id, is_bot=False, first_name='Test'),
                       date=date or self.before, text='not stored')

    def ids(self):
        return [r[0] for r in self.store.db.execute('SELECT message_id FROM owner_chat_messages ORDER BY message_id')]

    async def test_middleware_tracks_only_owner_private_and_real_bot_messages(self):
        incoming = OwnerIncomingMiddleware(self.job)
        handler = AsyncMock(return_value='ok')
        for message in (self.message(1), self.message(2, chat_id=88),
                        self.message(3, chat_type='group'), self.message(4, sender_id=88)):
            self.assertEqual(await incoming(handler, message, {'bot': SimpleNamespace(id=123)}), 'ok')
        await incoming(handler, self.message(8), {'bot': SimpleNamespace(id=999)})
        outgoing = OwnerOutgoingMiddleware(self.job)
        bot = SimpleNamespace(id=123)
        request = AsyncMock(return_value=self.message(5, sender_id=123))
        await outgoing(request, bot, SendMessage(chat_id=55, text='secret not stored'))
        request.return_value = self.message(6, chat_id=88)
        await outgoing(request, bot, SendMessage(chat_id=88, text='other chat'))
        self.assertEqual(self.ids(), [1, 5])
        columns = [row[1] for row in self.store.db.execute('PRAGMA table_info(owner_chat_messages)')]
        self.assertEqual(columns, ['bot_id', 'owner_id', 'message_id', 'created'])

    async def test_midnight_deletes_yesterday_only_and_sends_one_menu(self):
        self.job.remember(self.message(1), incoming=True)
        self.job.remember(self.message(2, sender_id=123), incoming=False)
        self.job.remember(self.message(3, date=self.after), incoming=True)
        self.store.set('active', True)
        self.store.set('catalog_version', 19)
        self.assertEqual((await self.job.cleanup_once(self.bot, self.menu, now=self.before))['status'], 'waiting')
        self.assertEqual((await self.job.cleanup_once(self.bot, self.menu, now=self.after))['status'], 'complete')
        self.bot.delete_messages.assert_awaited_once_with(chat_id=55, message_ids=[1, 2])
        self.menu.assert_awaited_once()
        self.assertEqual(self.ids(), [3])
        self.assertTrue(self.store.get('active'))
        self.assertEqual(self.store.get('catalog_version'), 19)
        self.assertEqual((await self.job.cleanup_once(self.bot, self.menu, now=self.after))['status'], 'waiting')
        self.menu.assert_awaited_once()

    async def test_restart_resumes_deletion_without_touching_today(self):
        self.job.remember(self.message(1), incoming=True)
        self.store.close()
        self.store = Store(self.path)
        self.job = OwnerChatCleanup(self.store, 55, 123)
        self.job.initialize(self.after)
        self.assertEqual((await self.job.cleanup_once(self.bot, self.menu, now=self.after))['status'], 'complete')
        self.bot.delete_messages.assert_awaited_once_with(chat_id=55, message_ids=[1])
        self.menu.assert_awaited_once()

    async def test_too_old_and_other_bot_records_are_never_deleted(self):
        self.job.remember(self.message(1, date=self.after - timedelta(hours=49)), incoming=True)
        self.job.remember(self.message(2), incoming=True)
        self.store.db.execute('INSERT INTO owner_chat_messages VALUES (?,?,?,?)', (999, 55, 3, self.before.timestamp()))
        self.store.db.execute('INSERT INTO owner_chat_messages VALUES (?,?,?,?)', (123, 66, 4, self.before.timestamp()))
        await self.job.cleanup_once(self.bot, self.menu, now=self.after)
        self.bot.delete_messages.assert_awaited_once_with(chat_id=55, message_ids=[2])
        self.assertEqual(self.ids(), [3, 4])

    async def test_rate_limit_retries_without_sending_menu(self):
        self.job.remember(self.message(1), incoming=True)
        self.bot.delete_messages.side_effect = [TelegramRetryAfter(DeleteMessages(chat_id=55, message_ids=[1]),
                                                                    'wait', retry_after=90), True]
        result = await self.job.cleanup_once(self.bot, self.menu, now=self.after)
        self.assertEqual((result['status'], result['retry_after']), ('retry', 90))
        self.assertEqual(self.ids(), [1])
        self.menu.assert_not_awaited()
        early = await self.job.cleanup_once(self.bot, self.menu, now=self.after + timedelta(seconds=30))
        self.assertEqual((early['status'], early['retry_after']), ('retry', 60))
        self.assertEqual(self.bot.delete_messages.await_count, 1)
        self.assertEqual((await self.job.cleanup_once(self.bot, self.menu, now=self.after + timedelta(seconds=90)))['status'], 'complete')
        self.menu.assert_awaited_once()

    async def test_network_failure_keeps_ids_for_next_attempt(self):
        self.job.remember(self.message(1), incoming=True)
        self.bot.delete_messages.side_effect = [OSError('unavailable'), True]
        self.assertEqual((await self.job.cleanup_once(self.bot, self.menu, now=self.after))['status'], 'retry')
        self.assertEqual(self.ids(), [1])
        self.menu.assert_not_awaited()
        self.assertEqual((await self.job.cleanup_once(self.bot, self.menu, now=self.after))['status'], 'complete')

    async def test_uncertain_menu_is_not_repeated_after_restart(self):
        self.menu.side_effect = OSError('timeout')
        self.assertEqual((await self.job.cleanup_once(self.bot, self.menu, now=self.after))['status'], 'uncertain')
        self.store.close()
        self.store = Store(self.path)
        self.job = OwnerChatCleanup(self.store, 55, 123)
        self.menu.reset_mock()
        self.assertEqual((await self.job.cleanup_once(self.bot, self.menu, now=self.after))['status'], 'waiting')
        self.menu.assert_not_awaited()

    async def test_batches_do_not_exceed_telegram_limit(self):
        for n in range(1, 103):
            self.job.remember(self.message(n), incoming=True)
        await self.job.cleanup_once(self.bot, self.menu, now=self.after)
        calls = self.bot.delete_messages.await_args_list
        self.assertEqual([len(call.kwargs['message_ids']) for call in calls], [100, 2])
        self.assertTrue(all(call.kwargs['chat_id'] == 55 for call in calls))

    async def test_real_aiogram_dispatch_and_outgoing_session_middleware(self):
        class StubSession(BaseSession):
            async def close(self):
                pass

            async def make_request(self, bot, method, timeout=None):
                return Message(message_id=99, date=self_timestamp,
                               chat=Chat(id=method.chat_id, type='private')).as_(bot)

            async def stream_content(self, *args, **kwargs):
                if False:
                    yield b''

        self_timestamp = self.before
        bot = Bot('123:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=StubSession())
        bot.session.middleware(OwnerOutgoingMiddleware(self.job))
        dispatcher = Dispatcher()
        dispatcher.message.outer_middleware(OwnerIncomingMiddleware(self.job))

        @dispatcher.message()
        async def reply(message):
            await message.answer('menu')

        await dispatcher.feed_update(bot, Update(update_id=1, message=self.message(7)))
        self.assertEqual(self.ids(), [7, 99])
        await bot.session.close()

    async def test_retry_deadline_survives_restart(self):
        self.job.remember(self.message(1), incoming=True)
        self.bot.delete_messages.side_effect = TelegramRetryAfter(
            DeleteMessages(chat_id=55, message_ids=[1]), 'wait', retry_after=90)
        await self.job.cleanup_once(self.bot, self.menu, now=self.after)
        self.store.close()
        self.store = Store(self.path)
        self.job = OwnerChatCleanup(self.store, 55, 123)
        result = await self.job.cleanup_once(self.bot, self.menu, now=self.after + timedelta(seconds=30))
        self.assertEqual(result, {'status': 'retry', 'retry_after': 60})
        self.bot.delete_messages.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
