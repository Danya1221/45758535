"""Small, deterministic replies in private dialogs with quoted buyers."""
from __future__ import annotations

import asyncio
import html
import re
import time

from .catalog import build_reply
from .engine import Incoming, SendFailure
from .followup_intent import classify_followup, resolve_hold


def user_tag(user) -> str:
    username = getattr(user, 'username', None)
    if isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{1,32}', username):
        return '@' + username
    name = ' '.join(filter(None, (getattr(user, 'first_name', None), getattr(user, 'last_name', None))))
    name = html.escape(name[:160] or 'Покупатель')
    user_id = getattr(user, 'id', 0)
    return f'<a href="tg://user?id={user_id}">{name}</a>' if isinstance(user_id, int) and user_id > 0 else name


class ConversationReplies:
    def __init__(self, store, send, notify, lock, *, max_per_minute=10, max_age=120, clock=time.time):
        self.store, self.send, self.notify, self.lock = store, send, notify, lock
        self.max_per_minute, self.max_age, self.clock = max_per_minute, max_age, clock

    async def process(self, message: Incoming, sender, reply_to_id: int | None = None) -> str:
        if (message.is_group or message.outgoing or message.is_bot or message.forwarded
                or message.sender_id <= 0 or message.chat_id != message.sender_id
                or getattr(sender, 'id', None) != message.sender_id
                or not message.text.strip() or len(message.text) > 4000):
            return 'ignored'
        # Check freshness at arrival, before waiting for an in-flight quote to
        # finish sending all its parts under the shared lock.
        if not -30 <= self.clock() - message.timestamp <= self.max_age:
            return 'old'
        async with self.lock:
            now = self.clock()
            account_id = self.store.get('telegram_account_id', 0)
            if not account_id or not self.store.get('active', False):
                return 'paused'
            if message.timestamp < self.store.get('monitor_enabled_at', 0):
                return 'before_enable'
            if reply_to_id is not None:
                quote = self.store.quote_for_reply(account_id, message.sender_id, reply_to_id, now=now)
            else:
                quote = self.store.latest_quote(account_id, message.sender_id, now=now)
            if quote is None or message.timestamp < int(quote['created']):
                return 'untracked'
            kind = classify_followup(message.text)
            last = self.store.latest_followup(account_id, message.sender_id, quote['quote_id'])
            waiting = last and last['kind'] == 'clarify' and last['status'] == 'sent'
            selected = None
            if kind == 'hold' or (kind is None and waiting):
                selected = resolve_hold(message.text, quote['products'])
                if kind is None and selected is None:
                    return 'ignored'
                kind = 'hold' if selected else 'clarify'
            if kind is None:
                return 'ignored'
            if self.store.sent_since(now - 60) >= self.max_per_minute:
                return 'rate_limited'
            if not self.store.reserve_followup(account_id, message.sender_id, message.message_id,
                                               quote['quote_id'], kind, now=now):
                return 'duplicate'
            holds = []
            if kind == 'hold':
                holds = self.store.reserve_hold(account_id, message.sender_id, quote['quote_id'], [selected], now=now)
            response = 'Нет' if kind == 'discount' else 'Ок'
            if kind == 'clarify':
                response = ('Какую именно позицию отложить? Напишите модель, память и цвет '
                            'или скопируйте нужную строку из списка.')
            elif kind == 'hold' and not holds:
                existing = self.store.holds(account_id, message.sender_id, quote['quote_id'])
                if not existing or any(row['status'] != 'sent' for row in existing):
                    response = 'Запрос на бронь требует подтверждения продавца.'
            try:
                sent_ids = await self.send(message.sender_id, message.message_id, response)
                if sent_ids:
                    # A buyer may reply directly to our clarification message,
                    # rather than to the original price message.
                    self.store.save_quote(account_id, message.sender_id, quote['chat_id'], quote['message_id'],
                                          quote['request_text'], quote['products'], now=now,
                                          sent_message_ids=sent_ids)
            except SendFailure as exc:
                status = 'uncertain' if exc.uncertain else 'failed'
                self.store.finish_followup(account_id, message.sender_id, message.message_id, status, str(exc))
                if holds:
                    self.store.finish_hold(account_id, message.sender_id, quote['quote_id'], holds, status, str(exc))
                if exc.uncertain or exc.pause:
                    self.store.set('active', False)
                return status
            except asyncio.CancelledError:
                self.store.finish_followup(account_id, message.sender_id, message.message_id, 'uncertain', 'cancelled_during_send')
                if holds:
                    self.store.finish_hold(account_id, message.sender_id, quote['quote_id'], holds, 'uncertain', 'cancelled_during_send')
                self.store.set('active', False)
                raise
            except Exception:
                self.store.finish_followup(account_id, message.sender_id, message.message_id, 'uncertain', 'unexpected_send_error')
                if holds:
                    self.store.finish_hold(account_id, message.sender_id, quote['quote_id'], holds, 'uncertain', 'unexpected_send_error')
                self.store.set('active', False)
                return 'uncertain'
            self.store.finish_followup(account_id, message.sender_id, message.message_id, 'sent')
            if holds:
                product_text = build_reply(holds, intro='')[:1500]
                request_text = quote['request_text'][:300]
                notice = (f'📌 Попросил отложить: {user_tag(sender)}\n'
                          f'{html.escape(product_text)}\n\nЗапрос: {html.escape(request_text)}')
                try:
                    notified = await self.notify(notice, parse_mode='HTML')
                except asyncio.CancelledError:
                    self.store.finish_hold(account_id, message.sender_id, quote['quote_id'], holds, 'uncertain', 'cancelled_during_notice')
                    self.store.set('active', False)
                    raise
                except Exception:
                    notified = False
                self.store.finish_hold(account_id, message.sender_id, quote['quote_id'], holds,
                                       'uncertain' if notified is False else 'sent',
                                       'owner_notification_failed' if notified is False else '')
                if notified is False:
                    self.store.set('active', False)
                    return 'notice_failed'
            return kind
