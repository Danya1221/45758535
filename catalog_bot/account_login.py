"""Owner-driven Telegram login; temporary credentials stay in memory."""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from telethon import errors

from .sessions import persist_session


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

    def cancel(self):
        self.generation += 1
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
            raise LoginError('Ввод истёк или был отменён. Начни заново: /login', restart=True)

    async def _connect(self):
        if not self.client.is_connected():
            await asyncio.wait_for(self.client.connect(), 20)

    async def _complete(self, generation=None):
        me = await asyncio.wait_for(self.client.get_me(), 15)
        if generation is not None:
            self._check_generation(generation)
        if me is None or getattr(me, 'bot', False):
            self.cancel()
            raise LoginError('Нужен пользовательский аккаунт Telegram. Начни заново: /login', restart=True)
        persist_session(self.client.session, self.data_dir)
        self.user, self.ready = me, True
        self.cancel()
        try:
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
                sent = await asyncio.wait_for(self.client.send_code_request(phone), 30)
                self._check_generation(generation)
                code_hash = getattr(sent, 'phone_code_hash', None)
                if not code_hash:
                    if await self.client.get_me():
                        await self._complete(generation)
                        return 'ready'
                    self.cancel()
                    raise LoginError('Telegram запросил другой способ подтверждения. Начни заново: /login', restart=True)
                self.phone, self.code_hash = phone, code_hash
                self.phase, self.deadline = 'code', self.clock() + 300
                return self.phase
            except errors.SessionPasswordNeededError:
                self._check_generation(generation)
                self.phase, self.deadline = 'password', self.clock() + 300
                return self.phase
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
                await asyncio.wait_for(self.client.sign_in(password=value), 30)
                await self._complete(generation)
                return 'ready'
            except Exception as exc:
                await self._failure(exc)

    async def _failure(self, exc):
        if isinstance(exc, LoginError):
            raise exc
        if isinstance(exc, errors.FloodWaitError):
            self.retry_until = self.clock() + exc.seconds
            raise LoginError(f'Telegram просит подождать {exc.seconds} секунд. Затем повтори ввод.') from None
        if isinstance(exc, (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError)):
            self.attempts += 1
            if self.attempts >= 5:
                self.cancel()
                raise LoginError('Слишком много неверных попыток. Начни заново: /login', restart=True) from None
            raise LoginError('Неверный код. Повтори ввод.' if isinstance(exc, errors.PhoneCodeInvalidError)
                             else 'Неверный пароль 2FA. Повтори ввод.') from None
        if isinstance(exc, errors.PhoneNumberInvalidError):
            raise LoginError('Telegram не принял номер. Проверь код страны и номер.') from None
        if isinstance(exc, errors.PhoneCodeExpiredError):
            self.cancel()
            raise LoginError('Код истёк. Начни заново: /login', restart=True) from None
        if isinstance(exc, (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.SessionExpiredError)):
            self.cancel()
            self.ready, self.user = False, None
            await self.client.disconnect()
            self.client.session.auth_key = None
            self.client.session.save()
            raise LoginError('Предыдущая авторизация недействительна. Начни заново: /login', restart=True) from None
        if isinstance(exc, (OSError, asyncio.TimeoutError)):
            raise LoginError('Нет связи с Telegram или не удалось сохранить авторизацию. Повтори ввод либо /login.') from None
        self.cancel()
        raise LoginError('Telegram отклонил вход. Начни заново: /login', restart=True) from None
