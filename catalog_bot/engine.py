from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .catalog import build_reply, find_products
from .storage import Store
from .pricing import price_products
from .offer_selection import select_offers


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
    def __init__(self, reason: str, uncertain: bool = False, *, pause: bool = True):
        super().__init__(reason)
        self.uncertain = uncertain
        self.pause = pause


class Engine:
    def __init__(self, store: Store, send: Callable[[int, int, str], Awaitable[list[int] | None]],
                 *, dedup_seconds: int = 120, max_per_minute: int = 10, max_age: int = 120,
                 contact: str = "", clock: Callable[[], float] = time.time,
                 reply_delay_seconds: float = 0):
        self.store, self.send = store, send
        self.dedup_seconds, self.max_per_minute, self.max_age = dedup_seconds, max_per_minute, max_age
        self.contact, self.clock = contact, clock
        self.lock = asyncio.Lock()
        self.reply_delay_seconds = reply_delay_seconds

    async def process(self, message: Incoming) -> str:
        if message.outgoing or message.is_bot or not message.is_group or message.forwarded or message.sender_id <= 0:
            return "ignored"
        if not message.text.strip() or len(message.text) > 12000:
            return "ignored"
        async with self.lock:
            now = self.clock()
            if self.store.is_excluded(message.sender_id):
                return 'excluded'
            if not self.store.get('active', False):
                return "paused"
            if not self.store.get('price_ready', True) and not self.store.quotable_products():
                return "price_unavailable"
            if message.chat_id not in self.store.chats():
                return "unselected"
            if not -30 <= now - message.timestamp <= self.max_age:
                return "old"
            version = self.store.get('catalog_version', 0)
            generation = self.store.get('catalog_generation', 0)
            manual_generation = self.store.get('manual_price_generation', 0)
            pricing_version = self.store.get('pricing_version', 0)
            account_id = self.store.get('telegram_account_id', 0)
            enabled_at = self.store.get('monitor_enabled_at', 0)
            quoted_products = price_products(self.store.quotable_products(), self.store.markup_policy())
            classification, found = await asyncio.to_thread(find_products, message.text, quoted_products,
                                                            self.store.get('allow_bare', True), regional_policy=True)
            if not found:
                return "sell" if classification == 'sell' else "no_match"
            found = select_offers(found)
            if self.store.delivery(message.chat_id, message.message_id):
                return 'duplicate'
            if self.reply_delay_seconds:
                await asyncio.sleep(self.reply_delay_seconds)
                now = self.clock()
                if not -30 <= now - message.timestamp <= self.max_age:
                    return 'old'
            # Pause, chat removal or inventory updates may happen while matching.
            if (not self.store.get('active', False) or message.chat_id not in self.store.chats()
                    or version != self.store.get('catalog_version', 0)
                    or pricing_version != self.store.get('pricing_version', 0)
                    or account_id != self.store.get('telegram_account_id', 0)
                    or enabled_at != self.store.get('monitor_enabled_at', 0)
                    or (not self.store.get('price_ready', True) and any(p.source == 'supplier' for p in found))):
                return "cancelled"
            if any(p.source == 'supernova' for p in found) and time.time() - self.store.get('supernova_updated_at', 0) >= 600:
                return 'cancelled'
            if self.store.is_excluded(message.sender_id):
                return 'excluded'
            if self.store.sent_since(now - 60) >= self.max_per_minute:
                return "rate_limited"
            if not self.store.reserve(message.chat_id, message.message_id, message.sender_id,
                                      message.text, self.dedup_seconds, now):
                return "duplicate"
            reply = build_reply(found, self.store.get('intro', 'Есть в наличии:'), self.contact)
            try:
                # Send to the requesting user. The second argument identifies
                # the source request; it is not a reply ID in the private chat.
                # Reservations and history remain keyed by the source group.
                sent_ids = await self.send(message.sender_id, message.message_id, reply)
                account_id = self.store.get('telegram_account_id', 0)
                if account_id:
                    self.store.save_quote(account_id, message.sender_id, message.chat_id, message.message_id,
                                          message.text, found, now=now, sent_message_ids=sent_ids or (),
                                          catalog_generation=generation, manual_price_generation=manual_generation)
            except SendFailure as exc:
                if str(exc) == 'excluded':
                    self.store.finish(message.chat_id, message.message_id, 'cancelled', 'excluded')
                    return 'excluded'
                self.store.finish(message.chat_id, message.message_id, 'uncertain' if exc.uncertain else 'failed', str(exc))
                if exc.uncertain or exc.pause:
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
