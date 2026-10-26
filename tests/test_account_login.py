import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors, functions
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from catalog_bot.account_login import AccountLogin, LoginError


class AccountClient:
    def __init__(self, session=None, *, two_factor=False):
        self.session = session if session is not None else StringSession()
        self.api_id, self.api_hash = 123, 'test'
        self.connected = False
        self.authorized = bool(self.session.auth_key)
        self.two_factor = two_factor
        self.expected_password = 'synthetic-password'
        self.user = SimpleNamespace(id=77, username='test_account', first_name='Test', bot=False)
        self.phone_calls, self.sign_calls, self.handlers = [], [], []
        self.send_error = None
        self.disconnected = asyncio.get_running_loop().create_future()

    def is_connected(self): return self.connected
    async def connect(self): self.connected = True
    async def disconnect(self): self.connected = False
    async def is_user_authorized(self): return self.authorized
    async def get_me(self): return self.user if self.authorized else None
    async def set_receive_updates(self, enabled): self.receive_updates = enabled
    def add_event_handler(self, *args): self.handlers.append(args)

    async def __call__(self, request):
        if isinstance(request, functions.auth.SendCodeRequest):
            return await self.send_code_request('+' + request.phone_number.removeprefix('+'))
        raise AssertionError('Unexpected request type')

    async def send_code_request(self, phone):
        self.phone_calls.append(phone)
        if self.send_error:
            raise self.send_error
        return SimpleNamespace(phone_code_hash='synthetic-private-code-hash')

    async def sign_in(self, **kwargs):
        self.sign_calls.append(kwargs)
        if 'password' in kwargs:
            if kwargs['password'] != self.expected_password:
                raise errors.PasswordHashInvalidError(request=None)
        else:
            if kwargs.get('code') != '12345':
                raise errors.PhoneCodeInvalidError(request=None)
            if self.two_factor:
                raise errors.SessionPasswordNeededError(request=None)
        self.session.set_dc(2, '149.154.167.51', 443)
        self.session.auth_key = AuthKey(bytes(range(256)))
        self.session.save()
        self.authorized = True
        return self.user

    async def iter_dialogs(self, **kwargs):
        yield SimpleNamespace(id=-100123, title='Тестовая группа', is_group=True)


class AccountLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.client = AccountClient()
        self.ready = AsyncMock()
        self.now = 1000
        self.auth = AccountLogin(self.client, self.directory, self.ready, clock=lambda: self.now)

    async def asyncTearDown(self):
        self.client.session.close()
        self.temp.cleanup()

    async def start_code(self):
        self.assertEqual(await self.auth.begin(), 'phone')
        self.assertEqual(await self.auth.phone_number('+7 (999) 123-45-67'), 'code')

    async def test_phone_code_persists_authorization_without_secrets_in_state(self):
        await self.start_code()
        self.assertFalse(self.auth.ready)
        self.assertEqual(await self.auth.code('1 2 3 4 5'), 'ready')
        self.assertTrue(self.auth.ready)
        self.assertEqual(self.client.phone_calls, ['+79991234567'])
        self.assertEqual(self.client.sign_calls[0]['code'], '12345')
        self.assertEqual(self.auth.phone, '')
        self.assertEqual(self.auth.code_hash, '')
        self.ready.assert_awaited_once()
        saved = SQLiteSession(str(self.directory / 'account'))
        self.assertEqual(saved.auth_key.key, bytes(range(256)))
        saved.close()
        self.assertEqual((self.directory / 'account.session').stat().st_mode & 0o777, 0o600)

    async def test_two_factor_retries_then_saves_only_authorized_key(self):
        self.client.two_factor = True
        await self.start_code()
        self.assertEqual(await self.auth.code('1 2 3 4 5'), 'password')
        self.assertFalse(self.auth.ready)
        with self.assertRaisesRegex(LoginError, 'Неверный пароль'):
            await self.auth.password('wrong-private-password')
        self.assertEqual(await self.auth.password('synthetic-password'), 'ready')
        self.assertNotIn(b'synthetic-password', (self.directory / 'account.session').read_bytes())
        self.assertEqual(self.auth.phase, '')

    async def test_invalid_code_does_not_advance_and_eventually_requires_restart(self):
        await self.start_code()
        for _ in range(4):
            with self.assertRaisesRegex(LoginError, 'Неверный код'):
                await self.auth.code('9 9 9 9 9')
            self.assertFalse(self.auth.ready)
        with self.assertRaises(LoginError) as error:
            await self.auth.code('9 9 9 9 9')
        self.assertTrue(error.exception.restart)
        self.assertEqual(self.auth.code_hash, '')
        self.ready.assert_not_awaited()

    async def test_invalid_phone_never_calls_telegram(self):
        await self.auth.begin()
        for phone in ('999', '/start', '+123', 'a+79991234567'):
            with self.assertRaises(LoginError):
                await self.auth.phone_number(phone)
        self.assertEqual(self.client.phone_calls, [])

    async def test_expired_flow_clears_phone_and_hash(self):
        await self.start_code()
        self.now += 301
        with self.assertRaisesRegex(LoginError, 'истёк'):
            await self.auth.code('1 2 3 4 5')
        self.assertEqual(self.auth.phone, '')
        self.assertEqual(self.auth.code_hash, '')
        self.assertEqual(self.client.sign_calls, [])

    async def test_expired_telegram_code_requires_new_login(self):
        await self.start_code()
        with patch.object(self.client, 'sign_in', AsyncMock(side_effect=errors.PhoneCodeExpiredError(request=None))):
            with self.assertRaises(LoginError) as error:
                await self.auth.code('1 2 3 4 5')
        self.assertTrue(error.exception.restart)
        self.assertEqual(self.auth.phase, '')

    async def test_flood_wait_is_respected_without_repeated_requests(self):
        await self.auth.begin()
        self.client.send_error = errors.FloodWaitError(request=None, capture=60)
        with self.assertRaisesRegex(LoginError, '60 секунд'):
            await self.auth.phone_number('+79991234567')
        with self.assertRaises(LoginError):
            await self.auth.phone_number('+79991234567')
        self.assertEqual(len(self.client.phone_calls), 1)
        self.now += 61
        self.client.send_error = None
        self.assertEqual(await self.auth.phone_number('+79991234567'), 'code')

    async def test_cancel_during_code_request_does_not_restore_old_flow(self):
        await self.auth.begin()
        started, finish = asyncio.Event(), asyncio.Event()
        async def delayed(phone):
            started.set()
            await finish.wait()
            return SimpleNamespace(phone_code_hash='late-private-hash')
        with patch.object(self.client, 'send_code_request', delayed):
            task = asyncio.create_task(self.auth.phone_number('+79991234567'))
            await started.wait()
            self.auth.cancel()
            finish.set()
            with self.assertRaises(LoginError):
                await task
        self.assertEqual(self.auth.phase, '')
        self.assertEqual(self.auth.code_hash, '')

    async def test_restore_from_volume_needs_no_new_code(self):
        await self.start_code()
        await self.auth.code('1 2 3 4 5')
        saved = SQLiteSession(str(self.directory / 'account'))
        client = AccountClient(saved)
        restored = AccountLogin(client, self.directory, AsyncMock())
        self.assertTrue(await restored.restore())
        self.assertEqual(await restored.begin(), 'ready')
        self.assertEqual(client.phone_calls, [])
        saved.close()

    async def test_unauthorized_boot_is_allowed_and_does_not_signal_ready(self):
        self.assertFalse(await self.auth.restore())
        self.assertTrue(self.client.connected)
        self.assertFalse(self.auth.ready)
        self.ready.assert_not_awaited()

    async def test_bot_account_is_rejected(self):
        self.client.user.bot = True
        await self.start_code()
        with self.assertRaisesRegex(LoginError, 'пользовательский аккаунт'):
            await self.auth.code('1 2 3 4 5')
        self.assertFalse(self.auth.ready)
        self.ready.assert_not_awaited()

    async def test_network_error_does_not_echo_phone_or_raw_exception(self):
        await self.auth.begin()
        self.client.send_error = OSError('private network details +79991234567')
        with self.assertRaises(LoginError) as error:
            await self.auth.phone_number('+79991234567')
        self.assertNotIn('private', str(error.exception))
        self.assertNotIn('7999', str(error.exception))
        self.assertEqual(self.auth.phase, 'phone')

    async def test_persistence_failure_does_not_signal_success(self):
        await self.start_code()
        with patch('catalog_bot.account_login.persist_session', side_effect=OSError('private storage error')):
            with self.assertRaises(LoginError):
                await self.auth.code('1 2 3 4 5')
        self.assertFalse(self.auth.ready)
        self.ready.assert_not_awaited()

    async def test_revoked_key_is_reset_and_new_login_can_start(self):
        await self.auth.begin()
        self.client.send_error = errors.AuthKeyUnregisteredError(request=None)
        with self.assertRaises(LoginError) as error:
            await self.auth.phone_number('+79991234567')
        self.assertTrue(error.exception.restart)
        self.assertEqual(self.auth.phase, '')
        self.assertIsNone(self.client.session.auth_key)
        self.client.send_error = None
        self.assertEqual(await self.auth.begin(), 'phone')

    async def test_known_telegram_rejections_have_actionable_diagnostics(self):
        cases = (
            (errors.ApiIdInvalidError, 'API_ID/API_HASH'),
            (errors.ApiIdPublishedFloodError, 'API_ID'),
            (errors.PhoneNumberBannedError, 'заблокирован'),
            (errors.PhoneNumberFloodError, 'Срок ожидания не указан'),
            (errors.PhonePasswordFloodError, 'Срок ожидания не указан'),
            (errors.PhoneNumberUnoccupiedError, 'нет зарегистрированного'),
            (errors.PhoneNumberAppSignupForbiddenError, 'существующий аккаунт'),
            (errors.PhoneCodeHashEmptyError, 'идентификатор'),
            (errors.AuthRestartError, 'заново'),
            (errors.SendCodeUnavailableError, 'не может отправить'),
            (errors.SmsCodeCreateFailedError, 'SMS'),
            (errors.UpdateAppToLoginError, 'обновления клиента'),
            (errors.AuthKeyDuplicatedError, 'одновременное'),
        )
        for error_type, expected in cases:
            with self.subTest(error=error_type.__name__):
                self.client.send_error = error_type(request=None)
                await self.auth.begin()
                with self.assertLogs('catalog_bot.account_login', level='WARNING') as logs:
                    with self.assertRaises(LoginError) as error:
                        await self.auth.phone_number('+79991234567')
                self.assertTrue(error.exception.restart)
                self.assertIn(expected, str(error.exception))
                self.assertIn(f'LOGIN/send_code/{error_type.__name__}', str(error.exception))
                self.assertIn(f'stage=send_code error={error_type.__name__}', logs.output[0])
                self.assertNotIn('79991234567', '\n'.join(logs.output) + str(error.exception))
                self.assertEqual(self.auth.phase, '')

    async def test_unknown_rpc_diagnostic_never_logs_server_text_or_request(self):
        await self.auth.begin()
        secrets = ('+79991234567', '98765', 'private-password', 'private-hash')
        request = SimpleNamespace(phone_number=secrets[0], phone_code=secrets[1],
                                  password=secrets[2], phone_code_hash=secrets[3])
        self.client.send_error = errors.RPCError(request, ' '.join(secrets), code=400)
        with self.assertLogs('catalog_bot.account_login', level='WARNING') as logs:
            with self.assertRaises(LoginError) as error:
                await self.auth.phone_number(secrets[0])
        output = str(error.exception) + '\n'.join(logs.output)
        self.assertIn('LOGIN/send_code/RPCError', output)
        self.assertIn('Telegram отклонил', output)
        for secret in secrets:
            self.assertNotIn(secret, output)

    async def test_duplicated_auth_key_is_discarded_before_new_login(self):
        await self.auth.begin()
        self.client.session.auth_key = AuthKey(bytes(reversed(range(256))))
        self.client.send_error = errors.AuthKeyDuplicatedError(request=None)
        with self.assertLogs('catalog_bot.account_login', level='WARNING'):
            with self.assertRaises(LoginError) as error:
                await self.auth.phone_number('+79991234567')
        self.assertTrue(error.exception.restart)
        self.assertIsNone(self.client.session.auth_key)
        self.assertFalse(self.auth.ready)
        self.assertFalse(self.client.connected)
        self.client.send_error = None
        self.assertEqual(await self.auth.begin(), 'phone')
        self.assertEqual(await self.auth.phone_number('+79991234567'), 'code')
        self.assertEqual(await self.auth.code('1 2 3 4 5'), 'ready')

    async def test_internal_error_is_not_misreported_as_telegram_rejection(self):
        await self.auth.begin()
        self.client.send_error = TypeError('private code 98765 and password')
        with self.assertLogs('catalog_bot.account_login', level='WARNING') as logs:
            with self.assertRaises(LoginError) as error:
                await self.auth.phone_number('+79991234567')
        output = str(error.exception) + '\n'.join(logs.output)
        self.assertIn('Внутренняя ошибка приложения', output)
        self.assertIn('LOGIN/send_code/TypeError', output)
        for private in ('Telegram отклонил', 'private', '98765'):
            self.assertNotIn(private, output)

    async def test_unregistered_exception_class_names_are_not_exposed(self):
        await self.auth.begin()
        self.client.send_error = type('PrivatePassword98765', (Exception,), {})('private body')
        with self.assertLogs('catalog_bot.account_login', level='WARNING') as logs:
            with self.assertRaises(LoginError) as error:
                await self.auth.phone_number('+79991234567')
        output = str(error.exception) + '\n'.join(logs.output)
        self.assertIn('LOGIN/send_code/InternalError', output)
        self.assertNotIn('PrivatePassword98765', output)
        self.assertNotIn('private body', output)

    async def test_failed_save_names_volume_and_correct_operation(self):
        await self.start_code()
        for error_type in (PermissionError, sqlite3.OperationalError):
            with self.subTest(error=error_type.__name__):
                with patch('catalog_bot.account_login.persist_session', side_effect=error_type('private file path')):
                    with self.assertLogs('catalog_bot.account_login', level='WARNING') as logs:
                        with self.assertRaises(LoginError) as error:
                            await self.auth.code('1 2 3 4 5')
                output = str(error.exception) + '\n'.join(logs.output)
                self.assertIn('Volume', output)
                self.assertIn('DATA_DIR', output)
                self.assertIn(f'LOGIN/save_session/{error_type.__name__}', output)
                self.assertNotIn('Нет связи с Telegram', output)
                self.assertNotIn('private file path', output)
                self.assertFalse(self.auth.ready)
                self.ready.assert_not_awaited()

    async def test_code_and_password_failures_report_the_actual_step(self):
        self.client.two_factor = True
        await self.start_code()
        with self.assertLogs('catalog_bot.account_login', level='WARNING'):
            with self.assertRaises(LoginError) as code_error:
                await self.auth.code('9 9 9 9 9')
        self.assertIn('LOGIN/check_code/PhoneCodeInvalidError', str(code_error.exception))
        self.assertEqual(await self.auth.code('1 2 3 4 5'), 'password')
        with self.assertLogs('catalog_bot.account_login', level='WARNING'):
            with self.assertRaises(LoginError) as password_error:
                await self.auth.password('wrong-private-password')
        self.assertIn('LOGIN/check_password/PasswordHashInvalidError', str(password_error.exception))
        self.assertNotIn('wrong-private-password', str(password_error.exception))

    async def test_premium_wait_is_respected_without_resending(self):
        await self.auth.begin()
        self.client.send_error = errors.FloodPremiumWaitError(request=None, capture=90)
        with self.assertLogs('catalog_bot.account_login', level='WARNING'):
            with self.assertRaisesRegex(LoginError, '90 секунд'):
                await self.auth.phone_number('+79991234567')
        with self.assertRaises(LoginError):
            await self.auth.phone_number('+79991234567')
        self.assertEqual(len(self.client.phone_calls), 1)

    async def test_transient_server_failure_preserves_phase_for_later_input(self):
        await self.auth.begin()
        self.client.send_error = errors.ServerError(None, 'private server details', code=500)
        with self.assertLogs('catalog_bot.account_login', level='WARNING'):
            with self.assertRaises(LoginError) as error:
                await self.auth.phone_number('+79991234567')
        self.assertFalse(error.exception.restart)
        self.assertIn('Временная ошибка', str(error.exception))
        self.assertNotIn('private server details', str(error.exception))
        self.assertEqual(self.auth.phase, 'phone')
        self.client.send_error = None
        self.assertEqual(await self.auth.phone_number('+79991234567'), 'code')
