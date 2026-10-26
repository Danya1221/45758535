"""Owner-driven Telegram login; temporary credentials stay in memory."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from pathlib import Path

from telethon import errors, functions, types

from .sessions import persist_session


log = logging.getLogger(__name__)

# Only names of classes exported by Telethon or known local exceptions may
# leave this module. str(exc), RPC request fields and arbitrary server text
# can contain authentication credentials and must never be logged/echoed.
_ERROR_NAMES = {
    value: name for name, value in vars(errors).items()
    if isinstance(value, type) and issubclass(value, Exception)
    and value.__module__.startswith('telethon.errors')
}
_ERROR_NAMES.update({value: value.__name__ for value in (
    OSError, PermissionError, ConnectionError, ConnectionResetError,
    ConnectionRefusedError, TimeoutError, ValueError, TypeError,
    AttributeError, RuntimeError, sqlite3.Error, sqlite3.OperationalError,
    sqlite3.DatabaseError, sqlite3.IntegrityError,
)})

_RESTART_MESSAGES = {
    errors.ApiIdInvalidError: 'Telegram не принял API_ID/API_HASH. Проверь оба значения из одного приложения на my.telegram.org в Variables Railway, затем обнови сервис.',
    errors.ApiIdPublishedFloodError: 'Telegram запретил использование этого API_ID. Нужны собственные API_ID и API_HASH из my.telegram.org; повторный /login с теми же данными не исправит запрет.',
    errors.PhoneNumberBannedError: 'Telegram сообщил, что номер заблокирован. Вход с ним недоступен; проверь аккаунт в официальном приложении Telegram.',
    errors.PhoneNumberFloodError: 'Telegram ограничил запросы кода для этого номера. Срок ожидания не указан. Не повторяй /login подряд; попробуй позднее.',
    errors.PhonePasswordFloodError: 'Telegram ограничил попытки ввода пароля. Срок ожидания не указан. Попробуй позднее.',
    errors.PhoneNumberUnoccupiedError: 'Для этого номера нет зарегистрированного аккаунта Telegram. Проверь номер и войди в существующий пользовательский аккаунт.',
    errors.PhoneNumberAppSignupForbiddenError: 'Для входа нужен существующий аккаунт Telegram. Регистрация нового аккаунта через этого бота не выполняется.',
    errors.PhoneCodeHashEmptyError: 'Telegram не получил идентификатор запроса кода. Начни заново: /login.',
    errors.AuthRestartError: 'Telegram потребовал начать авторизацию заново. Повтори /login позже.',
    errors.SendCodeUnavailableError: 'Telegram сейчас не может отправить код доступным способом. Используй /loginqr: подтверди вход через Telegram на телефоне. Не повторяй запрос кода подряд.',
    errors.SmsCodeCreateFailedError: 'Telegram не смог отправить SMS с кодом. Попробуй /login позднее.',
    errors.UpdateAppToLoginError: 'Telegram требует обновления клиента для входа. Сообщи разработчику код диагностики из этого сообщения.',
    errors.AuthKeyDuplicatedError: 'Telegram обнаружил одновременное использование одной авторизации. Останови другие экземпляры бота и оставь одну replica; затем повтори /login.',
}


class LoginError(Exception):
    def __init__(self, message: str, *, restart: bool = False):
        super().__init__(message)
        self.restart = restart


class AccountLogin:
    def __init__(self, client, data_dir: Path, on_ready, *, clock=time.monotonic):
        self.client, self.data_dir, self.on_ready, self.clock = client, data_dir, on_ready, clock
        self.lock = asyncio.Lock()
        self.ready = False
        self.user = None
        self.phase = ''
        self.phone = self.code_hash = ''
        self.deadline = self.retry_until = 0.0
        self.attempts = 0
        self.generation = 0
        self.operation = 'restore'
        self.qr = self.qr_waiter = None

    def cancel(self):
        self.generation += 1
        if self.qr_waiter and not self.qr_waiter.done():
            self.qr_waiter.cancel()
        self.qr = self.qr_waiter = None
        self.phase = self.phone = self.code_hash = ''
        self.deadline = 0
        self.attempts = 0

    def _check_generation(self, generation):
        if generation != self.generation:
            raise LoginError('Ввод отменён. Для нового входа: /login', restart=True)

    def _check_wait(self):
        wait = self.retry_until - self.clock()
        if wait > 0:
            raise LoginError(f'Telegram просит подождать {int(wait) + 1} секунд перед повторным входом.')

    def _check_phase(self, phase):
        self._check_wait()
        if self.phase != phase or self.clock() >= self.deadline:
            self.cancel()
            command = '/loginqr' if phase == 'qr' else '/login'
            raise LoginError(f'Ввод истёк или был отменён. Начни заново: {command}', restart=True)

    async def _connect(self):
        if not self.client.is_connected():
            self.operation = 'connect'
            await asyncio.wait_for(self.client.connect(), 20)

    async def _complete(self, generation=None):
        self.operation = 'get_account'
        me = await asyncio.wait_for(self.client.get_me(), 15)
        if generation is not None:
            self._check_generation(generation)
        if me is None or getattr(me, 'bot', False):
            self.cancel()
            raise LoginError('Нужен пользовательский аккаунт Telegram. Начни заново: /login', restart=True)
        self.operation = 'save_session'
        persist_session(self.client.session, self.data_dir)
        self.user, self.ready = me, True
        self.cancel()
        try:
            self.operation = 'activate_account'
            await self.on_ready(me)
        except Exception:
            self.ready = False
            raise
        return me

    async def _restore(self):
        if self.phase:
            if self.clock() >= self.deadline:
                self.cancel()
            else:
                return self.ready
        await self._connect()
        # get_me performs a real authorization check; is_user_authorized may
        # retain a cached True/False after the account state changes.
        self.operation = 'get_account'
        me = await asyncio.wait_for(self.client.get_me(), 15)
        if me is None or getattr(me, 'bot', False):
            self.ready = False
            self.user = None
            return False
        if not self.ready:
            await self._complete()
        return True

    async def restore(self):
        async with self.lock:
            return await self._restore()

    async def begin(self):
        async with self.lock:
            self._check_wait()
            self.cancel()
            generation = self.generation
            try:
                if await self._restore():
                    return 'ready'
            except Exception as exc:
                await self._failure(exc)
            self._check_generation(generation)
            self.phase, self.deadline = 'phone', self.clock() + 300
            return self.phase

    async def phone_number(self, value: str):
        async with self.lock:
            self._check_phase('phone')
            generation = self.generation
            phone = re.sub(r'[\s()\-]', '', value)
            if not re.fullmatch(r'\+[1-9]\d{6,14}', phone):
                raise LoginError('Пришли номер с кодом страны, например +79991234567.')
            try:
                await self._connect()
                self.operation = 'send_code'
                # A new owner flow must send a fresh request. Telethon's
                # convenience helper caches hashes across cancelled /login
                # flows and silently changes later calls into ResendCode.
                sent = await asyncio.wait_for(self.client(functions.auth.SendCodeRequest(
                    phone.removeprefix('+'), self.client.api_id, self.client.api_hash, types.CodeSettings())), 30)
                self._check_generation(generation)
                code_hash = getattr(sent, 'phone_code_hash', None)
                if not code_hash:
                    if await self.client.get_me():
                        await self._complete(generation)
                        return 'ready'
                    self.cancel()
                    raise LoginError('Telegram запросил другой способ подтверждения. Используй вход по QR: /loginqr', restart=True)
                self.phone, self.code_hash = phone, code_hash
                self.phase, self.deadline = 'code', self.clock() + 300
                return self.phase
            except errors.SessionPasswordNeededError:
                self._check_generation(generation)
                self.phase, self.deadline = 'password', self.clock() + 300
                return self.phase
            except Exception as exc:
                await self._failure(exc)

    async def _start_qr_waiter(self):
        # Install the update listener BEFORE a QR image reaches the owner.
        self.qr_waiter = asyncio.create_task(self.qr.wait())
        self.qr_waiter.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        await asyncio.sleep(0)

    async def begin_qr(self):
        async with self.lock:
            self._check_wait()
            self.cancel()
            generation = self.generation
            try:
                if await self._restore():
                    return None
                self.operation = 'create_qr'
                qr = await asyncio.wait_for(self.client.qr_login(), 30)
                self._check_generation(generation)
                self.qr = qr
                self.phase, self.deadline = 'qr', self.clock() + 300
                await self._start_qr_waiter()
                return generation
            except Exception as exc:
                await self._failure(exc)

    def qr_url(self, generation):
        self._check_generation(generation)
        self._check_phase('qr')
        return self.qr.url

    async def wait_qr(self, generation):
        async with self.lock:
            self._check_generation(generation)
            self._check_phase('qr')
            waiter = self.qr_waiter
            self.operation = 'wait_qr'
        try:
            await waiter
        except asyncio.TimeoutError:
            async with self.lock:
                self._check_generation(generation)
                self._check_phase('qr')
                self.operation = 'refresh_qr'
                try:
                    await asyncio.wait_for(self.qr.recreate(), 30)
                    self._check_generation(generation)
                    await self._start_qr_waiter()
                    return 'refresh'
                except Exception as exc:
                    await self._failure(exc)
        except errors.SessionPasswordNeededError:
            async with self.lock:
                self._check_generation(generation)
                self.qr = self.qr_waiter = None
                self.phase, self.deadline = 'password', self.clock() + 300
                return 'password'
        except Exception as exc:
            async with self.lock:
                self._check_generation(generation)
                await self._failure(exc)
        else:
            async with self.lock:
                self._check_generation(generation)
                try:
                    await self._complete(generation)
                    return 'ready'
                except Exception as exc:
                    await self._failure(exc)

    async def code(self, value: str):
        async with self.lock:
            self._check_phase('code')
            generation = self.generation
            # Spaces prevent Telegram from treating this chat message as a
            # forwarded login code. Never echo the supplied digits.
            if not re.fullmatch(r'[0-9\s]{4,30}', value) or not 4 <= len(re.sub(r'\s', '', value)) <= 8:
                raise LoginError('Введи только цифры кода через пробелы, например 1 2 3 4 5.')
            try:
                await self._connect()
                self.operation = 'check_code'
                await asyncio.wait_for(self.client.sign_in(phone=self.phone, code=re.sub(r'\s', '', value),
                                                          phone_code_hash=self.code_hash), 30)
                await self._complete(generation)
                return 'ready'
            except errors.SessionPasswordNeededError:
                self._check_generation(generation)
                self.phase, self.deadline = 'password', self.clock() + 300
                return self.phase
            except Exception as exc:
                await self._failure(exc)

    async def password(self, value: str):
        async with self.lock:
            self._check_phase('password')
            generation = self.generation
            if not value or len(value) > 1024:
                raise LoginError('Пришли пароль 2FA текстом или отмени вход: /cancel')
            try:
                await self._connect()
                self.operation = 'check_password'
                await asyncio.wait_for(self.client.sign_in(password=value), 30)
                await self._complete(generation)
                return 'ready'
            except Exception as exc:
                await self._failure(exc)

    async def _failure(self, exc):
        if isinstance(exc, LoginError):
            raise exc
        name = _ERROR_NAMES.get(type(exc), 'InternalError')
        # No exception object, traceback, request, phone, code or password.
        log.warning('Account login failed: stage=%s error=%s', self.operation, name)
        diagnostic = f'Код диагностики: LOGIN/{self.operation}/{name}'

        def failure(message, *, restart=False):
            return LoginError(f'{message}\n{diagnostic}', restart=restart)

        if isinstance(exc, (errors.FloodWaitError, errors.FloodPremiumWaitError)):
            self.retry_until = self.clock() + exc.seconds
            raise failure(f'Telegram просит подождать {exc.seconds} секунд. Затем повтори ввод.') from None
        if isinstance(exc, (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError)):
            self.attempts += 1
            if self.attempts >= 5:
                self.cancel()
                raise failure('Слишком много неверных попыток. Начни заново: /login', restart=True) from None
            raise failure('Неверный код. Повтори ввод.' if isinstance(exc, errors.PhoneCodeInvalidError)
                          else 'Неверный пароль 2FA. Повтори ввод.') from None
        if isinstance(exc, errors.PhoneNumberInvalidError):
            raise failure('Telegram не принял номер. Проверь код страны и номер.') from None
        if isinstance(exc, errors.PhoneCodeEmptyError):
            raise failure('Telegram не получил код входа. Повтори ввод цифрами через пробелы.') from None
        if isinstance(exc, errors.PhoneCodeExpiredError):
            self.cancel()
            raise failure('Код истёк. Начни заново: /login', restart=True) from None
        if isinstance(exc, (errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                            errors.SessionExpiredError, errors.AuthKeyDuplicatedError)):
            self.cancel()
            self.ready, self.user = False, None
            await self.client.disconnect()
            self.client.session.auth_key = None
            self.client.session.save()
            message = _RESTART_MESSAGES.get(type(exc), 'Предыдущая авторизация недействительна. Начни заново: /login')
            raise failure(message, restart=True) from None
        message = _RESTART_MESSAGES.get(type(exc))
        if message:
            self.cancel()
            raise failure(message, restart=True) from None
        if isinstance(exc, errors.ServerError):
            raise failure('Временная ошибка на стороне Telegram. Попробуй повторить ввод позднее.') from None
        if isinstance(exc, sqlite3.Error):
            self.cancel()
            raise failure('Не удалось сохранить данные аккаунта в базе. Проверь Volume и DATA_DIR в Railway. Сообщи разработчику код диагностики.', restart=True) from None
        if isinstance(exc, (OSError, asyncio.TimeoutError)):
            message = ('Не удалось сохранить авторизацию. Проверь подключение Volume, путь DATA_DIR и права записи в Railway.'
                       if self.operation in {'save_session', 'activate_account'} else 'Нет связи с Telegram. Повтори ввод либо /login.')
            raise failure(message) from None
        self.cancel()
        message = ('Telegram отклонил вход.' if isinstance(exc, errors.RPCError)
                   else 'Внутренняя ошибка приложения при входе.')
        raise failure(f'{message} Сообщи разработчику код диагностики. Для нового входа: /login', restart=True) from None
