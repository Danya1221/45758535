"""Persistent price polling preferences and the current runtime deadline."""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo('Europe/Moscow')


class PriceSchedule:
    def __init__(self, store, default_interval=600, *, clock=time.time):
        self.store, self.default_interval, self.clock = store, default_interval, clock
        self.next_at = self.clock() if self.enabled else None

    @property
    def enabled(self):
        return self.store.get('price_auto_enabled', True) is not False

    @property
    def interval(self):
        value = self.store.get('price_auto_interval', self.default_interval)
        return value if type(value) is int and 60 <= value <= 86400 else self.default_interval

    def configure(self, seconds):
        if seconds is None:
            self.store.set('price_auto_enabled', False)
        else:
            if type(seconds) is not int or not 60 <= seconds <= 86400:
                raise ValueError('Интервал: от 1 до 1440 минут.')
            self.store.set('price_auto_interval', seconds)
            self.store.set('price_auto_enabled', True)
        self.completed()

    def mark_due(self):
        self.next_at = self.clock() if self.enabled else None

    def completed(self):
        self.next_at = self.clock() + self.interval if self.enabled else None

    def remaining(self):
        if not self.enabled:
            return None
        return max(0, (self.next_at if self.next_at is not None else self.clock()) - self.clock())

    def auto_remaining(self, retry_after=0):
        """Next automatic request is within 10:00–20:00 Moscow time."""
        if not self.enabled:
            return None
        now = self.clock()
        due = max(now, self.next_at if self.next_at is not None else now, now + max(0, retry_after))
        local = datetime.fromtimestamp(due, MOSCOW)
        day_start = local.replace(hour=10, minute=0, second=0, microsecond=0)
        day_end = local.replace(hour=20, minute=0, second=0, microsecond=0)
        if local < day_start:
            due = day_start.timestamp()
        elif local >= day_end:
            due = (day_start + timedelta(days=1)).timestamp()
        return max(0, due - now)
