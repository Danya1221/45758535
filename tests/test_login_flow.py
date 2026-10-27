import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from telethon import errors
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from catalog_bot.app import Application, Config, run
from catalog_bot.storage import Store
from tests.test_account_login import AccountClient
from tests import test_app as controller_helpers
from tests.test_app import LocalSession


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    feed = controller_helpers.ControllerTests.feed
    texts = controller_helpers.ControllerTests.texts

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / 'catalog.sqlite3')
        self.transport = LocalSession()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.transport)
        self.client = AccountClient()
        self.config = Config(123, 'test', '', 55, self.directory)
        self.app = Application(self.config, self.store, self.client, self.bot)
        self.update_id = 0

    async def asyncTearDown(self):
        task = self.app.group_directory.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await self.bot.session.close()
        self.client.session.close()
        self.store.close()
        self.temp.cleanup()

    async def login_to_code(self):
        await self.feed('/login')
        await self.feed('+79991234567')

    async def test_boot_without_authorization_keeps_management_available(self):
        await self.feed('/start')
        self.assertIn('/login', self.texts()[-1])
        await self.feed('/status')
        self.assertIn('не подключён', self.texts()[-1])
        await self.feed('/chats')
        self.assertIn('/login', self.texts()[-1])
        await self.feed('/run')
        self.assertIn('/login', self.texts()[-1])
        self.assertFalse(self.store.get('active', False))

    async def test_owner_logs_in_and_then_selects_chats_without_local_commands(self):
        await self.login_to_code()
        self.assertIn('через пробелы', self.texts()[-1])
        await self.feed('1 2 3 4 5')
        self.assertIn('Аккаунт подключён', self.texts()[-1])
        self.assertTrue(self.app.auth.ready)
        self.assertTrue(self.app.account_available.is_set())
        self.assertEqual(self.store.get('telegram_account_id'), 77)
        self.assertTrue((self.directory / 'account.session').exists())
        self.assertFalse(self.store.get('active', False))
        self.assertTrue(any(call.__api_method__ == 'deleteMessage' for call in self.transport.calls))
        await self.feed('/chats')
        if self.app.group_directory.task:
            await self.app.group_directory.task
        self.assertIn('Группы: 1', self.texts()[-1])
        await self.feed('/login')
        self.assertIn('уже подключён', self.texts()[-1])
        self.assertEqual(len(self.client.phone_calls), 1)

    async def test_two_factor_password_is_not_echoed_and_messages_are_deleted(self):
        self.client.two_factor = True
        await self.login_to_code()
        await self.feed('1 2 3 4 5')
        self.assertIn('пароль 2FA', self.texts()[-1])
        await self.feed('wrong-password')
        self.assertIn('Неверный пароль', self.texts()[-1])
        await self.feed('synthetic-password')
        self.assertTrue(self.app.auth.ready)
        joined = '\n'.join(self.texts())
        for secret in ('wrong-password', 'synthetic-password', '1 2 3 4 5', 'synthetic-private-code-hash'):
            # The prompt contains a sample spaced code, not the secret input.
            if secret != '1 2 3 4 5':
                self.assertNotIn(secret, joined)
        self.assertEqual(sum(call.__api_method__ == 'deleteMessage' for call in self.transport.calls), 3)

    async def test_cancel_clears_secret_state_and_rejects_late_input(self):
        await self.login_to_code()
        await self.feed('/cancel')
        self.assertEqual(self.app.auth.phone, '')
        self.assertEqual(self.app.auth.code_hash, '')
        await self.feed('1 2 3 4 5')
        self.assertEqual(self.client.sign_calls, [])
        self.assertFalse(self.app.auth.ready)

    async def test_password_looking_like_command_is_not_executed(self):
        self.client.two_factor = True
        self.client.expected_password = '/run'
        await self.login_to_code()
        await self.feed('1 2 3 4 5')
        await self.feed('/run')
        self.assertTrue(self.app.auth.ready)
        self.assertFalse(self.store.get('active', False))
        self.assertEqual(self.client.sign_calls[-1], {'password': '/run'})

    async def test_other_user_or_group_cannot_request_codes_or_complete_login(self):
        await self.feed('/login', user_id=66)
        await self.feed('/login', chat_type='group')
        self.assertEqual(self.transport.calls, [])
        await self.login_to_code()
        await self.feed('1 2 3 4 5', user_id=66)
        await self.feed('1 2 3 4 5', chat_type='group')
        self.assertEqual(self.client.sign_calls, [])
        self.assertFalse(self.app.auth.ready)

    async def test_owner_receives_diagnostic_and_flow_is_cleared_after_api_error(self):
        await self.feed('/login')
        self.client.send_error = errors.ApiIdInvalidError(request=None)
        with self.assertLogs('catalog_bot.account_login', level='WARNING'):
            await self.feed('+79991234567')
        self.assertIn('API_ID/API_HASH', self.texts()[-1])
        self.assertIn('LOGIN/send_code/ApiIdInvalidError', self.texts()[-1])
        self.assertNotIn('79991234567', self.texts()[-1])
        await self.feed('+79991234567')
        self.assertEqual(len(self.client.phone_calls), 1)
        self.assertFalse(self.app.auth.ready)
        self.assertFalse(self.store.get('active', False))

    async def test_price_worker_waits_for_login_and_wakes_immediately(self):
        self.app.price_sync = type('Sync', (), {'refresh': AsyncMock(), 'retry_after': 0.0, 'peer': None})()
        task = asyncio.create_task(self.app.price_loop())
        try:
            await asyncio.sleep(0)
            self.app.price_sync.refresh.assert_not_awaited()
            await self.login_to_code()
            await self.feed('1 2 3 4 5')
            for _ in range(10):
                if self.app.price_sync.refresh.await_count:
                    break
                await asyncio.sleep(0)
            self.app.price_sync.refresh.assert_awaited_once()
            self.assertFalse(self.store.get('price_ready'))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_restored_account_preserves_run_mode_but_lost_authorization_pauses(self):
        await self.client.sign_in(code='12345')
        self.store.set('active', True)
        task = asyncio.create_task(self.app.account_loop())
        try:
            await asyncio.wait_for(self.app.account_available.wait(), 1)
            self.assertTrue(self.store.get('active'))
            self.client.authorized = False
            self.client.connected = False
            self.client.disconnected.set_result(None)
            self.app.account_changed.set()
            for _ in range(100):
                if not self.store.get('active'):
                    break
                await asyncio.sleep(0)
            self.assertFalse(self.store.get('active'))
            self.assertFalse(self.app.auth.ready)
            self.assertFalse(self.app.account_available.is_set())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_runtime_starts_polling_before_account_login_and_cleans_up(self):
        # Exercise run() itself with the real dispatcher and persistent session.
        directory = self.directory / 'runtime'
        directory.mkdir()
        config = Config(123, 'test', 'unused', 55, directory)
        captured = []
        original_app = Application
        def application(*args, **kwargs):
            app = original_app(*args, **kwargs)
            captured.append(app)
            return app
        def client_factory(path, *args, **kwargs):
            return AccountClient(SQLiteSession(path))
        async def polling(dispatcher, bot, **kwargs):
            app = captured[0]
            self.assertFalse(app.auth.ready)
            previous_app = self.app
            self.app = app
            try:
                await self.login_to_code()
                await self.feed('1 2 3 4 5')
                self.assertTrue(app.auth.ready)
            finally:
                self.app = previous_app
        with patch('catalog_bot.app.Application', side_effect=application), \
             patch('catalog_bot.app.TelegramClient', side_effect=client_factory), \
             patch('catalog_bot.app.Bot', return_value=self.bot), \
             patch.object(Dispatcher, 'start_polling', polling):
            await run(config)
        saved = SQLiteSession(str(directory / 'replies' / 'account'))
        self.assertIsNotNone(saved.auth_key)
        saved.close()
        captured[0].client.session.close()
        captured[0].price_client.session.close()
        store = Store(directory / 'catalog.sqlite3')
        self.assertEqual(store.get('telegram_account_id'), 77)
        self.assertFalse(store.get('active', False))
        store.close()

    async def test_runtime_keeps_legacy_session_for_prices_and_logs_in_new_reply_account(self):
        directory = self.directory / 'split-runtime'
        directory.mkdir()
        legacy = SQLiteSession(str(directory / 'account'))
        legacy.set_dc(2, '149.154.167.51', 443)
        legacy.auth_key = AuthKey(b'p' * 256)
        legacy.save()
        legacy.close()
        old_store = Store(directory / 'catalog.sqlite3')
        old_store.set('telegram_account_id', 77)
        old_store.set('telegram_account_name', 'supplier_access')
        old_store.toggle_chat(-100123, 'Старая группа')
        old_store.set('active', True)
        old_store.close()
        config = Config(123, 'test', 'unused', 55, directory, price_bot='@source_bot', price_request='/prices')
        captured = []
        original_app = Application

        def application(*args, **kwargs):
            app = original_app(*args, **kwargs)
            captured.append(app)
            return app

        def client_factory(path, *args, **kwargs):
            client = AccountClient(SQLiteSession(path))
            if Path(path).parent.name == 'replies':
                client.user.id, client.user.username = 88, 'new_seller'
            else:
                client.user.username = 'supplier_access'
            return client

        async def polling(dispatcher, bot, **kwargs):
            app = captured[0]
            await asyncio.wait_for(app.price_account_available.wait(), 2)
            self.assertTrue(app.price_auth.ready)
            self.assertFalse(app.auth.ready)
            self.assertIs(app.price_sync.client, app.price_client)
            self.assertEqual([callback.__self__ for callback, _ in app.client.handlers], [app, app])
            self.assertTrue(all(callback.__self__ is app.price_sync for callback, _ in app.price_client.handlers))
            self.assertEqual(len(app.price_client.handlers), 2)
            self.assertEqual(app.store.chats(), {})
            previous_app, self.app = self.app, app
            try:
                await self.login_to_code()
                await self.feed('1 2 3 4 5')
                self.assertEqual(app.auth.user.id, 88)
                self.assertEqual(app.store.get('telegram_account_id'), 88)
                self.assertEqual(app.store.get('price_account_id'), 77)
                self.assertEqual(app.price_client.phone_calls, [])
                self.assertEqual(app.price_client.session.auth_key.key, b'p' * 256)
                self.assertNotEqual(app.client.session.auth_key.key, app.price_client.session.auth_key.key)
            finally:
                self.app = previous_app

        with patch('catalog_bot.app.Application', side_effect=application), \
             patch('catalog_bot.app.TelegramClient', side_effect=client_factory), \
             patch('catalog_bot.app.Bot', return_value=self.bot), \
             patch('catalog_bot.app.PriceSync.refresh', new_callable=AsyncMock, return_value=True), \
             patch.object(Dispatcher, 'start_polling', polling):
            await run(config)
        captured[0].client.session.close()
        captured[0].price_client.session.close()


if __name__ == '__main__':
    unittest.main()
