"""Incremental, account-scoped group discovery with resumable FloodWait."""
import asyncio
import logging
import math
import time
import unicodedata

from telethon.errors import FloodWaitError, FloodPremiumWaitError

log = logging.getLogger(__name__)


def search_groups(items, query):
    def folded(text):
        return unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е')
    terms = folded(query).split()
    return [(cid, title) for cid, title in items.items()
            if all(term in folded(title) for term in terms)]


class GroupDirectory:
    def __init__(self, store, client, available, on_change, *, clock=time.time,
                 sleep=asyncio.sleep, page_pause=1):
        self.store, self.client, self.available, self.on_change = store, client, available, on_change
        self.clock, self.sleep, self.page_pause = clock, sleep, page_pause
        self.account_id = None
        self.items = {}
        self.complete = self.loading = False
        self.updated_at = self.retry_at = 0
        self.error = ''
        self.task = None
        self.generation = 0
        self.last_notice = 0
        self.bind(store.get('telegram_account_id'))

    def cancel(self):
        if self.loading:
            self.save()
        self.generation += 1
        task, self.task = self.task, None
        if task and not task.done():
            task.cancel()
        self.loading = False
        return task

    def bind(self, account_id):
        if account_id == self.account_id and self.account_id is not None:
            return
        self.cancel()
        self.account_id = account_id
        self.items = {}
        self.complete = False
        self.updated_at = self.retry_at = 0
        self.error = ''
        saved = self.store.get('group_directory', {})
        if not account_id or not isinstance(saved, dict) or saved.get('account_id') != account_id:
            return
        rows = saved.get('items', [])
        if isinstance(rows, list):
            self.items = {row[0]: row[1] for row in rows if isinstance(row, list) and len(row) == 2
                          and type(row[0]) is int and row[0] < 0 and isinstance(row[1], str)}
        self.complete = saved.get('complete') is True
        for key in ('updated_at', 'retry_at'):
            value = saved.get(key, 0)
            if type(value) in (int, float) and math.isfinite(value):
                setattr(self, key, value)

    def save(self):
        if self.account_id:
            self.store.set('group_directory', {
                'account_id': self.account_id, 'items': list(self.items.items()),
                'complete': self.complete, 'updated_at': self.updated_at, 'retry_at': self.retry_at,
            })

    def ensure_started(self, *, refresh=False):
        if self.task and not self.task.done():
            return self.task
        # Browsing is cached. Even explicit refresh clicks cannot repeatedly
        # enumerate all dialogs within a minute after a completed scan.
        if self.complete and self.clock() - self.updated_at < (60 if refresh else 900):
            return None
        self.loading, self.complete, self.error = True, False, ''
        generation = self.generation
        self.task = asyncio.create_task(self.load(generation))
        return self.task

    async def changed(self, *, force=False):
        if not force and self.clock() - self.last_notice < 1:
            return
        self.last_notice = self.clock()
        try:
            await self.on_change()
        except Exception as exc:
            log.warning('Group list notification failed: %s', type(exc).__name__)

    async def load(self, generation):
        fresh = {}
        try:
            # __aiter__ resets Telethon's cursor; invoke it exactly once.
            iterator = self.client.iter_dialogs(ignore_migrated=True).__aiter__()
            if hasattr(iterator, 'wait_time'):
                iterator.wait_time = self.page_pause
            await self.changed(force=True)
            while generation == self.generation:
                await self.available.wait()
                remaining = self.retry_at - self.clock()
                if remaining > 0:
                    await self.sleep(remaining)
                    continue
                try:
                    dialog = await asyncio.wait_for(anext(iterator), 30)
                except (FloodWaitError, FloodPremiumWaitError) as exc:
                    if generation != self.generation:
                        return
                    self.retry_at = max(self.retry_at, self.clock() + max(1, exc.seconds))
                    self.save()
                    await self.changed(force=True)
                    continue  # Resume this iterator, retaining its page offset.
                except StopAsyncIteration:
                    if generation != self.generation:
                        return
                    self.items = fresh
                    self.complete, self.loading = True, False
                    self.updated_at, self.retry_at = self.clock(), 0
                    self.save()
                    await self.changed(force=True)
                    return
                if generation != self.generation:
                    return
                if getattr(dialog, 'is_group', False):
                    cid, title = int(dialog.id), str(dialog.title or 'Без названия')
                    fresh[cid] = title
                    self.items[cid] = title
                    if len(fresh) % 25 == 0:
                        self.save()
                    await self.changed(force=len(fresh) in (1, 8))
        except asyncio.CancelledError:
            if generation == self.generation:
                self.save()
            raise
        except Exception as exc:
            if generation == self.generation:
                # Partial/cached groups stay usable; failure is never "0 groups".
                self.loading = False
                self.error = 'Не удалось закончить загрузку. Нажми «Обновить». Код: ' + type(exc).__name__
                self.retry_at = max(self.retry_at, self.clock() + 30)
                self.save()
                log.warning('Group list load failed: %s', type(exc).__name__)
                await self.changed(force=True)
        finally:
            if generation == self.generation:
                self.loading = False
