"""Daily cleanup of tracked messages in the owner's private bot chat.

Only message identifiers and dates observed since installation are stored.
No message bodies, other dialogs, catalog data or delivery history are touched.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import Message

log = logging.getLogger(__name__)
MOSCOW = ZoneInfo('Europe/Moscow')


class OwnerChatCleanup:
    def __init__(self, store, owner_id: int, bot_id: int):
        if owner_id <= 0 or bot_id <= 0:
            raise ValueError('owner_id and bot_id must be positive')
        self.store, self.owner_id, self.bot_id = store, owner_id, bot_id
        self.state_key = f'owner_cleanup:{bot_id}:{owner_id}'
        self.lock = asyncio.Lock()
        store.db.execute('''CREATE TABLE IF NOT EXISTS owner_chat_messages (
            bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(bot_id, owner_id, message_id))''')

    @staticmethod
    def _now(now=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError('cleanup time must include a timezone')
        return now.astimezone(timezone.utc)

    def initialize(self, now=None):
        # Installing the feature does not clear an already existing chat.
        today = self._now(now).astimezone(MOSCOW).date().isoformat()
        if self.store.get(self.state_key) is None:
            self.store.set(self.state_key, {'day': today, 'phase': 'complete'})

    def remember(self, message: Message, *, incoming: bool):
        chat = getattr(message, 'chat', None)
        sender = getattr(message, 'from_user', None)
        message_id, date = getattr(message, 'message_id', None), getattr(message, 'date', None)
        if (not chat or chat.type != 'private' or chat.id != self.owner_id
                or type(message_id) is not int or message_id <= 0 or not isinstance(date, datetime)
                or (incoming and (not sender or sender.id != self.owner_id))):
            return
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        self.store.db.execute('''INSERT OR IGNORE INTO owner_chat_messages
            (bot_id, owner_id, message_id, created) VALUES (?, ?, ?, ?)''',
            (self.bot_id, self.owner_id, message_id, date.timestamp()))

    async def cleanup_once(self, bot, send_menu, *, now=None):
        """Clean tracked previous-day messages once and send the daily menu.

        On an uncertain menu send, a durable marker avoids a duplicate menu on
        restart; Telegram does not offer idempotency keys for sendMessage.
        """
        fixed_time = now is not None
        now = self._now(now)
        today = now.astimezone(MOSCOW).date()
        day = today.isoformat()
        midnight = datetime.combine(today, datetime.min.time(), MOSCOW).timestamp()
        async with self.lock:
            state = self.store.get(self.state_key)
            if not state:
                self.initialize(now)
                return {'status': 'waiting'}
            if state.get('phase') == 'sending_menu':
                # A crash may have happened after Telegram accepted the menu.
                self.store.set(self.state_key, {'day': state['day'], 'phase': 'complete'})
                state = {'day': state['day'], 'phase': 'complete'}
            if state.get('phase') == 'complete' and state.get('day', '') >= day:
                return {'status': 'waiting'}
            remaining = math.ceil(state.get('retry_at', 0) - now.timestamp())
            if remaining > 0:
                return {'status': 'retry', 'retry_after': remaining}
            self.store.set(self.state_key, {'day': day, 'phase': 'deleting'})
            # Telegram only supports deleting messages younger than 48 hours.
            # Older known IDs are forgotten; they are not claimed as deleted.
            self.store.db.execute('''DELETE FROM owner_chat_messages WHERE bot_id=? AND owner_id=?
                AND created<=?''', (self.bot_id, self.owner_id, (now - timedelta(hours=48)).timestamp()))
            while True:
                rows = self.store.db.execute('''SELECT message_id FROM owner_chat_messages
                    WHERE bot_id=? AND owner_id=? AND created<? ORDER BY message_id LIMIT 100''',
                    (self.bot_id, self.owner_id, midnight)).fetchall()
                ids = [row[0] for row in rows]
                if not ids:
                    break
                try:
                    deleted = await bot.delete_messages(chat_id=self.owner_id, message_ids=ids)
                    if deleted is not True:
                        return {'status': 'retry', 'retry_after': 30}
                except TelegramRetryAfter as exc:
                    wait = max(1, exc.retry_after)
                    retry_from = now if fixed_time else self._now()
                    self.store.set(self.state_key, {'day': day, 'phase': 'deleting', 'retry_at': retry_from.timestamp() + wait})
                    return {'status': 'retry', 'retry_after': wait}
                except asyncio.CancelledError:
                    raise
                except (TelegramAPIError, OSError, asyncio.TimeoutError):
                    log.warning('Could not delete owner chat messages; will retry')
                    return {'status': 'retry', 'retry_after': 30}
                self.store.db.executemany('''DELETE FROM owner_chat_messages
                    WHERE bot_id=? AND owner_id=? AND message_id=?''',
                    [(self.bot_id, self.owner_id, mid) for mid in ids])
            self.store.set(self.state_key, {'day': day, 'phase': 'sending_menu'})
            try:
                await send_menu()
            except TelegramRetryAfter as exc:
                # Explicit flood refusal means Telegram did not accept the send.
                wait = max(1, exc.retry_after)
                retry_from = now if fixed_time else self._now()
                self.store.set(self.state_key, {'day': day, 'phase': 'deleting', 'retry_at': retry_from.timestamp() + wait})
                return {'status': 'retry', 'retry_after': wait}
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('Daily owner menu send could not be confirmed')
                # Result is uncertain. Do not automatically resend it.
                return {'status': 'uncertain'}
            self.store.set(self.state_key, {'day': day, 'phase': 'complete'})
            return {'status': 'complete'}

    async def run(self, bot, send_menu):
        self.initialize()
        while True:
            try:
                result = await self.cleanup_once(bot, send_menu)
                delay = max(1, min(60, int(result.get('retry_after', 30))))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Owner chat cleanup failed')
                delay = 30
            await asyncio.sleep(delay)


class OwnerIncomingMiddleware:
    def __init__(self, cleanup: OwnerChatCleanup):
        self.cleanup = cleanup

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and getattr(data.get('bot'), 'id', None) == self.cleanup.bot_id:
            try:
                self.cleanup.remember(event, incoming=True)
            except Exception:
                log.exception('Could not record owner chat message')
        return await handler(event, data)


class OwnerOutgoingMiddleware:
    def __init__(self, cleanup: OwnerChatCleanup):
        self.cleanup = cleanup

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        name = getattr(method, '__api_method__', '')
        if getattr(bot, 'id', None) == self.cleanup.bot_id and (name.startswith('send') or name in {'forwardMessage', 'copyMessage'}):
            for message in result if isinstance(result, list) else [result]:
                if isinstance(message, Message):
                    try:
                        self.cleanup.remember(message, incoming=False)
                    except Exception:
                        log.exception('Could not record sent owner chat message')
        return result
