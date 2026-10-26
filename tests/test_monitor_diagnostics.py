import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon.errors import FloodWaitError

from catalog_bot.catalog import Product
from catalog_bot.engine import Incoming
from catalog_bot.monitor_diagnostics import diagnose_chat, diagnose_message, reason_label
from catalog_bot.storage import Store


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'bot.sqlite3')
        self.store.replace_products([Product('Galaxy S25 12/256 Navy SM-S931B', Decimal('49000'), None)])
        self.store.toggle_chat(-100123, 'Тест')
        self.store.set('active', True)
        self.message = Incoming(-100123, 7, 55, 's25 navy', 1000)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def diagnose(self, message=None, **kwargs):
        return diagnose_message(self.store, message or self.message, now=1010, **kwargs)

    def test_unobserved_exact_match_identifies_missing_event_without_reserving(self):
        self.assertEqual(self.diagnose().result, 'not_observed')
        self.assertIn('совпадение есть, но событие не зафиксировано', reason_label('not_observed'))
        self.assertEqual(self.store.recent(), [])
        self.assertEqual(self.store.latest_checks(), [])

    def test_black_request_does_not_turn_into_navy(self):
        report = self.diagnose(replace(self.message, text='S25 256 black'))
        self.assertEqual(report.result, 'no_match')
        self.assertIn('цвет', report.current_match_details)

    def test_recorded_reason_precedes_current_settings_and_marks_observation(self):
        with patch('catalog_bot.storage.time.time', return_value=900):
            self.store.record_check(-100123, 7, 'rate_limited')
        report = self.diagnose(monitor_started_at=950)
        self.assertEqual(report.result, 'rate_limited')
        self.assertEqual(report.observed_at, 900)

    def test_recorded_no_match_keeps_outcome_when_current_catalog_now_matches(self):
        self.store.record_check(-100123, 7, 'no_match')
        report = self.diagnose()
        self.assertEqual(report.result, 'no_match')
        self.assertIn('Подходящих позиций сейчас: 1', report.current_match_details)
        black = replace(self.message, text='S25 256 black')
        self.assertIn('цвет', self.diagnose(black).current_match_details)

    def test_delivery_uncertainty_cannot_be_hidden_by_duplicate(self):
        self.store.reserve(-100123, 7, 55, self.message.text, 120, now=990)
        self.store.finish(-100123, 7, 'uncertain', 'unexpected_send_error')
        self.store.record_check(-100123, 7, 'duplicate')
        report = self.diagnose()
        self.assertEqual(report.result, 'uncertain')
        self.assertIn('повторная отправка заблокирована', reason_label(report.result))
        self.assertEqual(self.store.delivery(-100123, 7)['status'], 'uncertain')

    def test_current_guards_are_explained_without_claiming_event_reception(self):
        cases = [('own_message', {'outgoing': True}), ('bot', {'is_bot': True}),
                 ('forwarded', {'forwarded': True}), ('not_group', {'is_group': False}),
                 ('empty', {'text': ' '}), ('too_long', {'text': 'a' * 12001}),
                 ('missing_sender', {'sender_id': 0}), ('unselected', {'chat_id': -1}),
                 ('old', {'timestamp': 800})]
        for result, attrs in cases:
            with self.subTest(result=result):
                report = self.diagnose(replace(self.message, **attrs))
                self.assertEqual(report.result, result)
                self.assertIsNone(report.observed_at)
        self.assertEqual(self.diagnose(account_ready=False).result, 'account_unavailable')
        self.store.set('active', False)
        self.assertEqual(self.diagnose().result, 'paused')
        self.store.set('active', True)
        self.store.set('monitor_enabled_at', 1001)
        self.assertEqual(self.diagnose().result, 'before_enable')
        self.store.set('monitor_enabled_at', 0)
        self.assertEqual(self.diagnose(monitor_started_at=1001).result, 'before_start')
        self.store.set('price_ready', False)
        self.assertEqual(self.diagnose().result, 'price_unavailable')

    def test_seller_messages_and_bare_disabled_are_not_missing_events(self):
        self.assertEqual(self.diagnose(replace(self.message, text='Продам Samsung S25 256 Navy')).result, 'sell')
        self.store.set('allow_bare', False)
        report = self.diagnose()
        self.assertEqual(report.result, 'bare_disabled')
        self.assertIn('Названия без «куплю» отключены', report.current_match_details)
        self.store.record_check(-100123, 7, 'no_match')
        report = self.diagnose()
        self.assertEqual(report.result, 'no_match')
        self.assertIn('Названия без «куплю» отключены', report.current_match_details)

    def test_new_request_in_same_telegram_second_as_start_is_not_before_start(self):
        report = self.diagnose(monitor_started_at=1000.9)
        self.assertEqual(report.result, 'not_observed')
        self.assertEqual(self.diagnose(monitor_started_at=1001).result, 'before_start')

    def test_checks_are_bounded_and_success_time_survives_duplicate_event(self):
        with patch('catalog_bot.storage.time.time', return_value=900):
            self.store.record_check(-100123, 7, 'sent')
        with patch('catalog_bot.storage.time.time', return_value=901):
            self.store.record_check(-100123, 7, 'received', 'should_not_replace')
        saved = self.store.message_check(-100123, 7)
        self.assertEqual((saved['result'], saved['observed'], saved['detail']), ('sent', 900, ''))
        with patch('catalog_bot.storage.time.time', return_value=902):
            for message_id in range(20, 325):
                self.store.record_check(-100123, message_id, 'no_match', 'x' * 300)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM message_checks').fetchone()[0], 300)
        self.assertIsNone(self.store.message_check(-100123, 7))
        self.assertIsNone(self.store.message_check(-100123, 20))
        self.assertEqual(self.store.latest_checks(-100123)[0]['message_id'], 324)
        self.assertEqual(len(self.store.message_check(-100123, 324)['detail']), 120)

    async def test_history_reader_is_selected_only_read_only_and_caps_limit(self):
        calls = []
        raw = SimpleNamespace(id=7, raw_text='s25 navy', date=datetime.fromtimestamp(1000, timezone.utc),
                              out=False, sender_id=55, sender=SimpleNamespace(bot=False), fwd_from=None)
        class Client:
            async def get_messages(self, chat_id, limit):
                calls.append((chat_id, limit))
                return [raw]
        text = await diagnose_chat(Client(), self.store, -100123, now=1010, limit=500)
        self.assertEqual(calls, [(-100123, 5)])
        self.assertIn('совпадение есть, но событие не зафиксировано', text)
        self.assertIn('Событие не зафиксировано', text)
        self.assertEqual(self.store.recent(), [])
        self.assertEqual(self.store.latest_checks(), [])
        self.assertIn('не выбрана', await diagnose_chat(Client(), self.store, -456))
        self.assertEqual(calls, [(-100123, 5)])

    async def test_history_shows_old_record_as_pre_restart_result(self):
        raw = SimpleNamespace(id=7, raw_text='s25 navy', date=datetime.fromtimestamp(850, timezone.utc),
                              out=False, sender_id=55, sender=None, fwd_from=None)
        class Client:
            async def get_messages(self, chat_id, limit):
                return [raw]
        with patch('catalog_bot.storage.time.time', return_value=900):
            self.store.record_check(-100123, 7, 'no_match')
        text = await diagnose_chat(Client(), self.store, -100123, now=1010, monitor_started_at=950)
        self.assertIn('Зафиксированный результат до перезапуска', text)
        self.assertNotIn('Событие не зафиксировано', text)

    async def test_history_error_never_discloses_request_or_exception_text(self):
        class Client:
            async def get_messages(self, chat_id, limit):
                raise OSError('secret token or phone')
        text = await diagnose_chat(Client(), self.store, -100123)
        self.assertIn('OSError', text)
        self.assertNotIn('secret', text)
        self.assertEqual(reason_label('error', 'secret token'), 'ошибка при обработке сообщения')

    async def test_flood_error_is_bounded_and_does_not_touch_active_flag(self):
        class Client:
            async def get_messages(self, chat_id, limit):
                raise FloodWaitError(None, capture=20)
        text = await diagnose_chat(Client(), self.store, -100123)
        self.assertIn('FloodWaitError', text)
        self.assertTrue(self.store.get('active'))
        self.assertEqual(self.store.latest_checks(), [])


if __name__ == '__main__':
    unittest.main()
