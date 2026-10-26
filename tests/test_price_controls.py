import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from catalog_bot.app import Application, Config, menu
from catalog_bot.price_schedule import PriceSchedule
from catalog_bot.storage import Store
from tests import test_app as controller_helpers
from tests.test_price_sync import SourceClient, PRICE


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'catalog.sqlite3'
        self.store = Store(self.path)
        self.now = 1000
        self.schedule = PriceSchedule(self.store, 600, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_deadline_is_counted_from_end_of_refresh(self):
        self.assertEqual(self.schedule.remaining(), 0)
        self.now += 120
        self.schedule.completed()
        self.assertEqual(self.schedule.remaining(), 600)
        self.now += 150
        self.assertEqual(self.schedule.remaining(), 450)

    def test_setting_and_disable_survive_database_reopen(self):
        self.schedule.configure(1800)
        self.schedule.configure(None)
        self.store.close()
        self.store = Store(self.path)
        restored = PriceSchedule(self.store, 60, clock=lambda: self.now)
        self.assertEqual(restored.interval, 1800)
        self.assertFalse(restored.enabled)
        self.assertIsNone(restored.remaining())
        restored.configure(restored.interval)
        self.assertEqual(restored.remaining(), 1800)

    def test_invalid_interval_does_not_change_previous_rule(self):
        self.schedule.configure(900)
        for value in (0, 59, 86401, True, 1.5, '600'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.schedule.configure(value)
                self.assertEqual(self.schedule.interval, 900)
                self.assertTrue(self.schedule.enabled)

    def test_manual_refresh_cannot_reenable_disabled_timer(self):
        self.schedule.configure(None)
        self.schedule.completed()
        self.schedule.mark_due()
        self.assertIsNone(self.schedule.remaining())
        self.assertIsNone(self.schedule.next_at)


class PriceControlTests(unittest.IsolatedAsyncioTestCase):
    feed = controller_helpers.ControllerTests.feed
    texts = controller_helpers.ControllerTests.texts

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / 'catalog.sqlite3')
        self.transport = controller_helpers.LocalSession()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.transport)
        self.client = SourceClient()
        self.client.is_connected = lambda: True
        self.config = Config(123, 'test', '', 55, self.directory,
                             price_bot='@tech_online_seller_bot', price_request='/prices')
        self.app = Application(self.config, self.store, self.client, self.bot)
        self.client.sync = self.app.price_sync
        self.app.price_sync.timeout = .03
        self.app.auth.ready = True
        self.app.account_available.set()
        self.now = 1000
        self.app.price_schedule.clock = lambda: self.now
        self.app.price_schedule.mark_due()
        self.update_id = 0
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.bot.session.close()
        self.store.close()
        self.temp.cleanup()

    async def callback(self, action, *, user_id=55, chat_type='private'):
        self.update_id += 1
        message = Message(message_id=900, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type=chat_type))
        callback = CallbackQuery(id=str(self.update_id), from_user=User(id=user_id, is_bot=False, first_name='Test'),
                                 chat_instance='test', message=message, data=action)
        await self.app.dp.feed_update(self.bot, Update(update_id=self.update_id, callback_query=callback))

    async def wait_calls(self, count):
        async def poll():
            while len(self.client.calls) < count:
                await asyncio.sleep(.005)
        await asyncio.wait_for(poll(), 3)

    def start_loop(self):
        task = asyncio.create_task(self.app.price_loop())
        self.tasks.append(task)
        return task

    async def test_main_price_button_requests_supplier_and_imports_full_price(self):
        labels = {b.callback_data: b.text for row in menu().inline_keyboard for b in row}
        self.assertEqual(labels['price'], '💰 Запросить прайс')
        self.assertIn('uploadprice', labels)
        self.assertIn('price_timer', labels)
        await self.callback('price')
        self.assertEqual(self.client.calls, ['/prices'])
        self.assertEqual(len(self.store.products()), 1)
        self.assertTrue(self.store.get('price_ready'))
        self.assertIn('Прайс обновлён', self.texts()[-1])
        self.assertFalse(any('Пришли прайс' in text for text in self.texts()))
        self.assertEqual(self.app.price_schedule.remaining(), 600)

    async def test_price_and_refresh_commands_both_request_supplier(self):
        await self.feed('/price')
        await self.feed('/refresh')
        self.assertEqual(self.client.calls, ['/prices', '/prices'])

    async def test_manual_upload_is_separate_and_never_contacts_supplier(self):
        await self.callback('uploadprice')
        self.assertIn('Пришли прайс', self.texts()[-1])
        await self.feed(PRICE)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(self.store.products()), 1)

    async def test_timer_panel_presets_and_custom_input_apply_without_restart(self):
        await self.feed('/price')
        await self.callback('price_timer')
        self.assertIn('10 мин', self.texts()[-1])
        self.assertIn('@tech_online_seller_bot', self.texts()[-1])
        await self.callback('timer:30')
        self.assertEqual(self.app.price_schedule.interval, 1800)
        self.assertEqual(self.app.price_schedule.remaining(), 1800)
        await self.callback('timer:custom')
        await self.feed('7')
        self.assertEqual(self.app.price_schedule.interval, 420)
        self.assertIn('07:00', self.texts()[-1])
        self.assertEqual(PriceSchedule(self.store, 60).interval, 420)

    async def test_off_stops_auto_requests_but_manual_button_keeps_working(self):
        await self.feed('/timer off')
        self.start_loop()
        await asyncio.sleep(.02)
        self.assertEqual(self.client.calls, [])
        await self.callback('price')
        await asyncio.sleep(.02)
        self.assertEqual(self.client.calls, ['/prices'])
        self.assertFalse(self.app.price_schedule.enabled)
        self.assertIsNone(self.app.price_schedule.next_at)

    async def test_background_timer_repeats_and_new_interval_is_used(self):
        self.start_loop()
        await self.wait_calls(1)
        self.now += 601
        self.app.refresh_requested.set()
        await self.wait_calls(2)
        await self.feed('/timer 30')
        self.now += 1799
        self.app.refresh_requested.set()
        await asyncio.sleep(.02)
        self.assertEqual(len(self.client.calls), 2)
        self.now += 1
        self.app.refresh_requested.set()
        await self.wait_calls(3)
        self.assertEqual(self.client.calls, ['/prices'] * 3)

    async def test_timer_respects_telegram_wait_before_requesting_source(self):
        self.app.price_sync.retry_after = time.monotonic() + 3600
        self.start_loop()
        await asyncio.sleep(.02)
        self.assertEqual(self.client.calls, [])
        self.app.price_sync.retry_after = 0
        self.app.refresh_requested.set()
        await self.wait_calls(1)

    async def test_manual_request_and_due_timer_do_not_duplicate_source_command(self):
        release = asyncio.Event()
        async def delayed(peer, text, **kwargs):
            self.client.calls.append(text)
            await release.wait()
            await self.client.emit()
        self.client.send_message = delayed
        request = asyncio.create_task(self.feed('/price'))
        self.tasks.append(request)
        await self.wait_calls(1)
        self.start_loop()
        await asyncio.sleep(.02)
        self.assertTrue(self.app.price_sync.lock.locked())
        release.set()
        await asyncio.wait_for(asyncio.shield(request), 3)
        await asyncio.sleep(.02)
        self.assertEqual(self.client.calls, ['/prices'])
        self.assertEqual(self.app.price_schedule.remaining(), 600)

    async def test_failed_fetch_keeps_old_catalog_and_schedules_later_retry(self):
        await self.feed('/price')
        self.client.response = PRICE.replace('1 товаров', '425 товаров')
        self.now += 60
        await self.feed('/price')
        self.assertEqual(len(self.store.products()), 1)
        self.assertFalse(self.store.get('price_ready'))
        self.assertEqual(self.app.price_schedule.remaining(), 600)

    async def test_invalid_and_unauthorized_timer_changes_leave_rule_unchanged(self):
        await self.feed('/timer 15')
        for value in ('-1', '1441', '1.5', 'NaN', '9999999999999'):
            await self.feed('/timer ' + value)
            self.assertEqual(self.app.price_schedule.interval, 900)
        await self.feed('/timer 1', user_id=66)
        await self.feed('/timer off', chat_type='group')
        await self.callback('timer:1', user_id=66)
        self.assertEqual(self.app.price_schedule.interval, 900)
        self.assertTrue(self.app.price_schedule.enabled)

    async def test_status_shows_countdown_and_manual_button_does_not_need_timer(self):
        await self.feed('/price')
        self.now += 65
        await self.feed('/status')
        self.assertIn('08:55', self.texts()[-1])
        await self.feed('/timer off')
        await self.feed('/status')
        self.assertIn('Автообновление: выключено', self.texts()[-1])

    async def test_source_request_requires_authorized_account(self):
        self.app.auth.ready = False
        await self.callback('price')
        self.assertEqual(self.client.calls, [])
        self.assertIn('/login', self.texts()[-1])
