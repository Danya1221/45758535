from __future__ import annotations

import asyncio
import time
import re
from typing import TYPE_CHECKING

from telethon.tl.types import ButtonTypeDefault, InlineButtonTypeCallback
from telethon.errors import FloodWaitError

from .prices import FOOTER, PriceFeedError, parse_price_feed

if TYPE_CHECKING:
    from .storage import Store


class PriceCollector:
    """A complete response to one refresh request; edited chunks replace old text."""
    def __init__(self):
        self.parts: dict[int, str] = {}
        self.active = False
        self.boundary = 0
        self.edit_id: int | None = None
        self.error = ''

    def begin(self, boundary: int, edit_id: int | None = None):
        self.parts.clear()
        self.active, self.boundary, self.edit_id, self.error = True, boundary, edit_id, ''

    def add(self, message_id: int, text: str):
        if not self.active or (message_id < self.boundary and message_id != self.edit_id):
            return None
        # Ignore menus and progress messages sent as separate messages.
        if not (re.search(r'\bот\s+\d+\s*шт', text, re.I) or FOOTER.search(text)
                or re.search(r'^\s*(?:\*\*)?\s*[•▪]', text, re.M)):
            return None
        self.parts[message_id] = text
        joined = '\n'.join(self.parts[key] for key in sorted(self.parts))
        if len(joined.encode('utf-8')) > 1_000_000:
            self.active = False
            raise PriceFeedError('Ответ источника больше 1 МБ')
        if not FOOTER.search(joined):
            return None
        try:
            snapshot = parse_price_feed(joined, require_complete=True)
        except PriceFeedError as exc:
            self.error = str(exc)
            return None
        self.active = False
        return snapshot


class PriceSync:
    def __init__(self, store: Store, client, username: str, notify, *, request_text: str = '',
                 button_text: str = '', interval: int = 600, timeout: int = 120):
        self.store, self.client, self.username, self.notify = store, client, username, notify
        self.request_text, self.button_text, self.interval, self.timeout = request_text, button_text, interval, timeout
        self.peer = None
        self.collector = PriceCollector()
        self.done = asyncio.Event()
        self.lock = asyncio.Lock()
        self.failure = ''
        self.retry_after = 0.0

    async def receive(self, event):
        if not self.peer or event.chat_id != self.peer.id or event.out or not self.collector.active:
            return
        try:
            snapshot = self.collector.add(event.id, event.raw_text or '')
            if snapshot is None:
                return
            was_ready = self.store.get('price_ready', True)
            summary = self.store.apply_price_snapshot(snapshot, 'bot:' + str(self.peer.id))
            self.done.set()
            if summary['baseline'] or summary['added'] or summary['changed'] or summary['removed'] or not was_ready:
                await self.notify(format_summary(summary))
        except Exception as exc:
            self.failure = str(exc) if isinstance(exc, PriceFeedError) else type(exc).__name__
            self.done.set()

    async def resolve(self):
        peer = await self.client.get_entity(self.username)
        if not getattr(peer, 'bot', False):
            raise PriceFeedError('Источник должен быть Telegram-ботом')
        self.peer = peer

    async def refresh(self):
        if self.lock.locked():
            return False
        if time.monotonic() < self.retry_after:
            await self.notify(f'Telegram ограничил запросы прайса. Повтор возможен через {int(self.retry_after - time.monotonic()) + 1} секунд.')
            return False
        async with self.lock:
            self.done.clear()
            self.failure = ''
            try:
                if self.peer is None:
                    await self.resolve()
                messages = await self.client.get_messages(self.peer, limit=20)
                boundary = max((m.id for m in messages), default=0) + 1
                target = None
                if self.button_text:
                    for message in messages:
                        for row in message.buttons or []:
                            for button in row:
                                if button.text == self.button_text and isinstance(getattr(button.button, 'type', None), (ButtonTypeDefault, InlineButtonTypeCallback)):
                                    target = message
                                    break
                            if target:
                                break
                        if target:
                            break
                    if target is None:
                        raise PriceFeedError('В последних сообщениях не найдена настроенная кнопка прайса')
                self.collector.begin(boundary, edit_id=target.id if target else None)
                if target:
                    await target.click(text=self.button_text)
                else:
                    await self.client.send_message(self.peer, self.request_text, parse_mode=None)
                await asyncio.wait_for(self.done.wait(), timeout=self.timeout)
                if self.failure:
                    raise PriceFeedError(self.failure)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.set('price_ready', False)
                if isinstance(exc, FloodWaitError):
                    self.retry_after = time.monotonic() + exc.seconds
                detail = (self.collector.error or 'Не получен полный прайс') if isinstance(exc, asyncio.TimeoutError) else (
                    str(exc) if isinstance(exc, PriceFeedError) else type(exc).__name__)
                await self.notify('Прайс не обновлён: ' + detail + '\nПредыдущая версия сохранена. Ответы по ценам временно приостановлены до успешного обновления.')
                return False
            finally:
                self.collector.active = False

    async def run(self):
        while True:
            await self.refresh()
            await asyncio.sleep(max(self.interval, self.retry_after - time.monotonic()))


def format_summary(summary: dict) -> str:
    if summary['baseline']:
        text = f"📥 Первый прайс сохранён: {summary['total']} предложений. Это исходная версия для сравнения."
    else:
        text = (f"🔄 Прайс обновлён. Предложений: {summary['total']}\n"
                f"Изменились цены: {summary['changed']}; добавлено: {summary['added']}; исчезло: {summary['removed']}.")
    if not summary['complete']:
        text += '\nЗагружена часть прайса: отсутствующие в этой части товары не удалялись.'
    if summary['duplicates']:
        text += f"\nОбъединено точных дублей: {summary['duplicates']}."
    if summary['warnings']:
        text += f"\nСпорных строк: {summary['warnings']}; они исключены из автоматических предложений."
    text += '\nНаличие и количество на складе источник не указал. Изменения: /changes.'
    return text
