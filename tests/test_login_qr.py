import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from png import Reader
from telethon import TelegramClient, errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.custom import QRLogin

from catalog_bot.account_login import AccountLogin, LoginError
from catalog_bot.login_qr import qr_png
from tests.test_account_login import AccountClient
from tests import test_login_flow as flow_helpers


class LocalQR:
    def __init__(self, client):
        self.client = client
        self.recreates = 0
        self.wait_started = asyncio.Event()
        self.future = asyncio.get_running_loop().create_future()

    @property
    def url(self):
        return f'tg://login?token=synthetic-private-token-{self.recreates}'

    async def recreate(self):
        self.recreates += 1
        self.wait_started.clear()
        self.future = asyncio.get_running_loop().create_future()

    async def wait(self):
        self.wait_started.set()
        await self.future
        self.client.session.set_dc(2, '149.154.167.51', 443)
        self.client.session.auth_key = AuthKey(bytes(range(256)))
        self.client.authorized = True
        return self.client.user

    def approve(self):
        if self.client.two_factor:
            self.future.set_exception(errors.SessionPasswordNeededError(request=None))
        else:
            self.future.set_result(None)


class QRLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.client = AccountClient()
        self.qr = LocalQR(self.client)
        self.client.qr_login = AsyncMock(return_value=self.qr)
        self.ready = AsyncMock()
        self.now = 1000
        self.auth = AccountLogin(self.client, self.directory, self.ready, clock=lambda: self.now)

    async def asyncTearDown(self):
        task = self.auth.qr_waiter
        self.auth.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        self.client.session.close()
        self.temp.cleanup()

    async def test_qr_listener_precedes_display_and_success_survives_restart(self):
        generation = await self.auth.begin_qr()
        self.assertTrue(self.qr.wait_started.is_set())
        self.assertEqual(self.auth.qr_url(generation), self.qr.url)
        self.assertEqual(self.client.phone_calls, [])
        self.qr.approve()
        self.assertEqual(await self.auth.wait_qr(generation), 'ready')
        self.assertTrue(self.auth.ready)
        self.assertIsNone(self.auth.qr)
        self.assertIsNone(self.auth.qr_waiter)
        self.ready.assert_awaited_once()
        saved = SQLiteSession(str(self.directory / 'account'))
        try:
            restored = AccountLogin(AccountClient(saved), self.directory, AsyncMock())
            self.assertTrue(await restored.restore())
        finally:
            saved.close()

    async def test_qr_two_factor_requires_password_before_persistence(self):
        self.client.two_factor = True
        generation = await self.auth.begin_qr()
        self.qr.approve()
        self.assertEqual(await self.auth.wait_qr(generation), 'password')
        self.assertFalse(self.auth.ready)
        self.assertFalse((self.directory / 'account.session').exists())
        self.assertIsNone(self.auth.qr)
        self.assertEqual(await self.auth.password('synthetic-password'), 'ready')
        self.assertTrue((self.directory / 'account.session').exists())

    async def test_expired_token_refreshes_until_total_flow_deadline(self):
        generation = await self.auth.begin_qr()
        old_url = self.auth.qr_url(generation)
        self.qr.future.set_exception(asyncio.TimeoutError())
        self.assertEqual(await self.auth.wait_qr(generation), 'refresh')
        self.assertNotEqual(self.auth.qr_url(generation), old_url)
        self.assertTrue(self.qr.wait_started.is_set())
        self.now += 301
        with self.assertRaisesRegex(LoginError, '/loginqr'):
            await self.auth.wait_qr(generation)
        self.assertIsNone(self.auth.qr)
        self.assertFalse(self.auth.ready)

    async def test_cancel_removes_waiter_and_late_result_cannot_save_session(self):
        generation = await self.auth.begin_qr()
        waiter = self.auth.qr_waiter
        self.auth.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        self.assertTrue(waiter.cancelled())
        with self.assertRaises(LoginError):
            self.auth.qr_url(generation)
        with self.assertRaises(LoginError):
            await self.auth.wait_qr(generation)
        self.assertFalse((self.directory / 'account.session').exists())
        self.ready.assert_not_awaited()

    async def test_qr_wait_failure_never_echoes_or_logs_token(self):
        generation = await self.auth.begin_qr()
        self.qr.future.set_exception(TypeError(self.qr.url))
        with self.assertLogs('catalog_bot.account_login', level='WARNING') as logs:
            with self.assertRaises(LoginError) as error:
                await self.auth.wait_qr(generation)
        output = str(error.exception) + '\n'.join(logs.output)
        self.assertIn('LOGIN/wait_qr/TypeError', output)
        self.assertNotIn('synthetic-private-token', output)
        self.assertNotIn('tg://', output)

    async def test_qr_preserves_telegram_wait_and_does_not_repeat_request(self):
        self.client.qr_login.side_effect = errors.FloodWaitError(None, capture=60)
        with self.assertLogs('catalog_bot.account_login', level='WARNING'):
            with self.assertRaisesRegex(LoginError, '60 секунд'):
                await self.auth.begin_qr()
        with self.assertRaises(LoginError):
            await self.auth.begin_qr()
        self.assertEqual(self.client.qr_login.await_count, 1)
        self.assertEqual(self.client.phone_calls, [])

    async def test_existing_authorization_does_not_export_another_qr(self):
        self.client.authorized = True
        self.client.session.set_dc(2, '149.154.167.51', 443)
        self.client.session.auth_key = AuthKey(bytes(range(256)))
        self.assertIsNone(await self.auth.begin_qr())
        self.client.qr_login.assert_not_awaited()
        self.assertTrue(self.auth.ready)

    async def test_new_phone_flow_uses_sendcode_despite_telethon_cached_hash(self):
        class RecordedClient(TelegramClient):
            def __init__(self):
                super().__init__(StringSession(), 123, 'synthetic-hash')
                self.requests = []
                self._phone = '79991234567'
                self._phone_code_hash[self._phone] = 'obsolete-private-hash'

            def is_connected(self): return True
            async def get_me(self): return None

            async def __call__(self, request, *args, **kwargs):
                self.requests.append(request)
                if isinstance(request, functions.auth.ResendCodeRequest):
                    raise errors.SendCodeUnavailableError(request)
                if isinstance(request, functions.auth.SendCodeRequest):
                    return types.auth.SentCode(types.auth.SentCodeTypeApp(5), 'fresh-private-hash')
                raise AssertionError('Unexpected request')

        client = RecordedClient()
        try:
            # Reproduce the original helper failure with its stale hash.
            with self.assertRaises(errors.SendCodeUnavailableError):
                await client.send_code_request('+79991234567')
            client.requests.clear()
            auth = AccountLogin(client, self.directory, AsyncMock())
            for _ in range(2):
                await auth.begin()
                self.assertEqual(await auth.phone_number('+79991234567'), 'code')
                self.assertEqual(auth.code_hash, 'fresh-private-hash')
                auth.cancel()
            self.assertEqual(len(client.requests), 2)
            self.assertTrue(all(isinstance(r, functions.auth.SendCodeRequest) for r in client.requests))
            self.assertTrue(all(r.phone_number == '79991234567' for r in client.requests))
        finally:
            client.session.close()

    async def test_real_telethon_qr_wait_handles_dc_migration_and_two_factor(self):
        from datetime import datetime, timedelta, timezone

        class QRProtocolClient(AccountClient):
            async def qr_login(self):
                qr = QRLogin(self, [])
                await qr.recreate()
                return qr

            async def __call__(self, request):
                if isinstance(request, functions.auth.ExportLoginTokenRequest):
                    if not self.handlers:
                        return types.auth.LoginToken(datetime.now(timezone.utc) + timedelta(seconds=30), b'synthetic-qr-token')
                    raise AssertionError('The QR wait listener must be removed before importing')
                if isinstance(request, functions.auth.ImportLoginTokenRequest):
                    if self.two_factor:
                        raise errors.SessionPasswordNeededError(request)
                    return types.auth.LoginTokenSuccess(types.auth.Authorization(user=self.user))
                raise AssertionError('Unexpected QR request')

            def remove_event_handler(self, handler):
                self.handlers[:] = [h for h in self.handlers if h[0] != handler]

            async def _switch_dc(self, dc_id):
                self.switched_dc = dc_id

            async def _on_login(self, user):
                self.session.set_dc(4, '149.154.167.91', 443)
                self.session.auth_key = AuthKey(bytes(range(256)))
                self.authorized = True
                return user

        for two_factor in (False, True):
            with self.subTest(two_factor=two_factor):
                client = QRProtocolClient(two_factor=two_factor)
                auth = AccountLogin(client, self.directory, AsyncMock())
                # After the update, ExportLoginToken migrates the real QRLogin
                # object to another DC, then ImportLoginToken authorizes it.
                original_call = client.__class__.__call__
                accepted = False
                async def request_handler(instance, request):
                    if isinstance(request, functions.auth.ExportLoginTokenRequest) and accepted:
                        return types.auth.LoginTokenMigrateTo(4, b'synthetic-migration-token')
                    return await original_call(instance, request)
                try:
                    from unittest.mock import patch
                    with patch.object(QRProtocolClient, '__call__', request_handler):
                        generation = await auth.begin_qr()
                        self.assertEqual(len(client.handlers), 1)
                        accepted = True
                        for handler, _ in list(client.handlers):
                            await handler(types.UpdateLoginToken())
                        result = await auth.wait_qr(generation)
                        self.assertEqual(result, 'password' if two_factor else 'ready')
                        self.assertEqual(client.switched_dc, 4)
                        self.assertEqual(client.handlers, [])
                        if two_factor:
                            self.assertEqual(await auth.password('synthetic-password'), 'ready')
                        self.assertTrue(auth.ready)
                finally:
                    auth.cancel()
                    client.session.close()

    async def test_rendered_qr_is_a_nonempty_black_white_png(self):
        payload = qr_png(self.qr.url)
        width, height, rows, _ = Reader(bytes=payload).asRGB8()
        self.assertEqual(width, height)
        self.assertGreater(width, 200)
        self.assertLess(width, 1000)
        colors = {value for row in rows for value in row}
        self.assertEqual(colors, {0, 255})


class QRFlowTests(unittest.IsolatedAsyncioTestCase):
    feed = flow_helpers.LoginFlowTests.feed
    texts = flow_helpers.LoginFlowTests.texts

    async def asyncSetUp(self):
        await flow_helpers.LoginFlowTests.asyncSetUp(self)
        self.qr = LocalQR(self.client)
        self.client.qr_login = AsyncMock(return_value=self.qr)

    async def asyncTearDown(self):
        task = self.app.cancel_login()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await flow_helpers.LoginFlowTests.asyncTearDown(self)

    async def wait_method(self, name):
        async def poll():
            while not any(call.__api_method__ == name for call in self.transport.calls):
                await asyncio.sleep(.01)
        await asyncio.wait_for(poll(), 5)

    async def start_qr(self):
        await self.feed('/loginqr')
        task = self.app.qr_task
        await self.wait_method('sendPhoto')
        return task

    async def test_owner_gets_protected_qr_then_success_and_photo_is_removed(self):
        task = await self.start_qr()
        photo = next(c for c in self.transport.calls if c.__api_method__ == 'sendPhoto')
        self.assertEqual(photo.chat_id, 55)
        self.assertTrue(photo.protect_content)
        self.assertTrue(photo.photo.data.startswith(b'\x89PNG'))
        self.assertNotIn('tg://', photo.caption)
        self.assertTrue(self.qr.wait_started.is_set())
        self.qr.approve()
        await task
        self.assertIn('Аккаунт подключён', self.texts()[-1])
        self.assertTrue(self.app.auth.ready)
        self.assertTrue(self.app.account_available.is_set())
        self.assertFalse(self.store.get('active', False))
        self.assertTrue((self.directory / 'account.session').exists())
        self.assertTrue(any(c.__api_method__ == 'deleteMessage' for c in self.transport.calls))
        self.assertFalse((self.directory / 'login_qr.png').exists())

    async def test_other_users_and_groups_cannot_export_or_receive_qr(self):
        await self.feed('/loginqr', user_id=66)
        await self.feed('/loginqr', chat_type='group')
        self.client.qr_login.assert_not_awaited()
        self.assertEqual(self.transport.calls, [])

    async def test_qr_two_factor_uses_existing_password_flow(self):
        self.client.two_factor = True
        task = await self.start_qr()
        self.qr.approve()
        await task
        self.assertIn('пароль 2FA', self.texts()[-1])
        self.assertFalse(self.app.auth.ready)
        await self.feed('synthetic-password')
        self.assertTrue(self.app.auth.ready)
        self.assertIn('Аккаунт подключён', self.texts()[-1])

    async def test_cancel_and_phone_login_stop_qr_without_overwriting_new_state(self):
        task = await self.start_qr()
        await self.feed('/login')
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.app.auth.phase, 'phone')
        self.assertIsNone(self.app.qr_task)
        self.assertFalse(self.app.auth.ready)
        self.assertTrue(any(c.__api_method__ == 'deleteMessage' for c in self.transport.calls))
        await self.feed('/cancel')
        self.assertEqual(self.app.auth.phase, '')

    async def test_expired_qr_edits_same_photo_instead_of_spamming_chat(self):
        await self.start_qr()
        self.qr.future.set_exception(asyncio.TimeoutError())
        await self.wait_method('editMessageMedia')
        self.assertEqual(sum(c.__api_method__ == 'sendPhoto' for c in self.transport.calls), 1)
        self.assertEqual(self.qr.recreates, 1)
        self.assertEqual(self.app.auth.phase, 'qr')

    async def test_qr_command_during_password_input_is_not_used_as_password(self):
        self.client.two_factor = True
        await self.feed('/login')
        await self.feed('+79991234567')
        await self.feed('1 2 3 4 5')
        self.assertEqual(self.app.auth.phase, 'password')
        await self.start_qr()
        self.assertEqual(self.app.auth.phase, 'qr')
        self.assertFalse(any(c.get('password') == '/loginqr' for c in self.client.sign_calls))

    async def test_cancel_command_cancels_listener_and_deletes_image(self):
        task = await self.start_qr()
        waiter = self.app.auth.qr_waiter
        await self.feed('/cancel')
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(waiter.cancelled())
        self.assertEqual(self.app.auth.phase, '')
        self.assertIn('Ввод отменён', self.texts()[-1])
        self.assertTrue(any(c.__api_method__ == 'deleteMessage' for c in self.transport.calls))
