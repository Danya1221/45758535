"""Receive an Excel export sent by Supernova after the user opens its Mini App."""
import asyncio
import logging
import time

from .prices import PriceFeedError
from .supernova import MAX_FILE, parse_supernova_xlsx

log = logging.getLogger(__name__)


class SupernovaSync:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.peer_id = None
        self.lock = asyncio.Lock()

    async def receive(self, event):
        if not event.is_private or event.out or getattr(event, 'fwd_from', None):
            return
        file = event.file
        if not file or not (file.name or '').casefold().endswith('.xlsx') or not file.size or file.size > MAX_FILE:
            return
        observed = event.date.timestamp()
        if not time.time() - 120 <= observed <= time.time() + 30:
            return
        try:
            async with self.lock:
                if self.peer_id is None:
                    peer = await self.client.get_entity('@supernova_bot')
                    if not getattr(peer, 'bot', False):
                        return
                    self.peer_id = peer.id
                if event.chat_id != self.peer_id or event.sender_id != self.peer_id:
                    return
                data = await self.client.download_media(event.message, file=bytes)
                if not isinstance(data, bytes) or len(data) > MAX_FILE:
                    return
                snapshot = await asyncio.to_thread(parse_supernova_xlsx, data)
                self.store.apply_extra_snapshot(snapshot, 'supernova', event.id, observed)
        except (PriceFeedError, OSError, ValueError) as exc:
            log.warning('Supernova export rejected: %s', type(exc).__name__)
