from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .catalog import build_reply, find_products
from .storage import Store


@dataclass(frozen=True)
class Incoming:
    chat_id: int
    message_id: int
    sender_id: int
    text: str
    timestamp: float
    outgoing: bool = False
    is_bot: bool = False
    is_group: bool = True
    forwarded: bool = False


class SendFailure(Exception):
    def __init__(self, reason: str, uncertain: bool = False):
        super().__init__(reason)
        self.uncertain = uncertain


class Engine:
    def __init__(self, store: Store, send: Callable[[int, int, str], Awaitable[None]],
                 *, dedup_seconds: int = 120, max_per_minute: int = 10, max_age: int = 120,
                 contact: str = "", clock: Callable[[], float] = time.time):
        self.store, self.send = store, send
        self.dedup_seconds, self.max_per_minute, self.max_age = dedup_seconds, max_per_minute, max_age
        self.contact, self.clock = contact, clock
        self.lock = asyncio.Lock()

    async def process(self, message: Incoming) -> str:
        if message.outgoing or message.is_bot or not message.is_group or message.forwarded or not message.sender_id:
            return "ignored"
        if not message.text.strip() or len(message.text) > 12000:
            return "ignored"
        async with self.lock:
            now = self.clock()
            if not self.store.get('active', False):
                return "paused"
            if not self.store.get('price_ready', True):
                return "price_unavailable"
            if message.chat_id not in self.store.chats():
                return "unselected"
            if not -30 <= now - message.timestamp <= self.max_age:
                return "old"
            version = self.store.get('catalog_version', 0)
            classification, found = await asyncio.to_thread(find_products, message.text, self.store.products(), self.store.get('allow_bare', True))
            if not found:
                return "sell" if classification == 'sell' else "no_match"
            # Pause, chat removal or inventory updates may happen while matching.
            if (not self.store.get('active', False) or message.chat_id not in self.store.chats()
                    or version != self.store.get('catalog_version', 0) or not self.store.get('price_ready', True)):
                return "cancelled"
            if self.store.sent_since(now - 60) >= self.max_per_minute:
                return "rate_limited"
            if not self.store.reserve(message.chat_id, message.message_id, message.sender_id,
                                      message.text, self.dedup_seconds, now):
                return "duplicate"
            reply = build_reply(found, self.store.get('intro', 'Есть в наличии:'), self.contact)
            try:
                await self.send(message.chat_id, message.message_id, reply)
            except SendFailure as exc:
                self.store.finish(message.chat_id, message.message_id, 'uncertain' if exc.uncertain else 'failed', str(exc))
                self.store.set('active', False)
                return "uncertain" if exc.uncertain else "failed"
            except asyncio.CancelledError:
                self.store.finish(message.chat_id, message.message_id, 'uncertain', 'cancelled_during_send')
                self.store.set('active', False)
                raise
            except Exception:
                # Unknown errors may happen after Telegram has accepted the message.
                self.store.finish(message.chat_id, message.message_id, 'uncertain', 'unexpected_send_error')
                self.store.set('active', False)
                return "uncertain"
            self.store.finish(message.chat_id, message.message_id, 'sent')
            return "sent"
