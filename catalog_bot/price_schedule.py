"""Persistent price polling preferences and the current runtime deadline."""
import time


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
