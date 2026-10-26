import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_bot.catalog import parse_catalog
from catalog_bot.engine import Engine, Incoming, SendFailure
from catalog_bot.storage import Store


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.store = Store(self.path)
        self.store.replace_products(parse_catalog('iPhone 17 256GB Black SIM|85000|2'))
        self.store.toggle_chat(-100123, 'Покупки')
        self.store.toggle_chat(-100124, 'Покупки 2')
        self.store.set('active', True)
        self.sent = []

        async def send(recipient, source_message, text):
            self.sent.append((recipient, source_message, text))

        self.engine = Engine(self.store, send, clock=lambda: 1000)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def incoming(self, **changes):
        data = dict(chat_id=-100123, message_id=1, sender_id=55, text='Куплю iPhone 17', timestamp=1000)
        return Incoming(**(data | changes))

    async def test_quote_goes_to_requesting_user_and_history_keeps_source_group(self):
        self.assertEqual(await self.engine.process(self.incoming()), 'sent')
        self.assertEqual(self.sent[0][:2], (55, 1))
        self.assertIn('85 000 ₽', self.sent[0][2])
        delivery = self.store.delivery(-100123, 1)
        self.assertEqual((delivery['sender_id'], delivery['status']), (55, 'sent'))
        self.assertIsNone(self.store.delivery(55, 1))

    async def test_duplicate_concurrent_deliveries_only_send_once(self):
        results = await asyncio.gather(*(self.engine.process(self.incoming()) for _ in range(5)))
        self.assertEqual(results.count('sent'), 1)
        self.assertEqual(len(self.sent), 1)

    async def test_same_buyer_crossposting_deduplicated(self):
        await self.engine.process(self.incoming())
        self.assertEqual(await self.engine.process(self.incoming(chat_id=-100124, message_id=2)), 'duplicate')
        self.assertEqual(await self.engine.process(self.incoming(message_id=3, sender_id=56)), 'sent')
        self.assertEqual([row[0] for row in self.sent], [55, 56])

    async def test_unselected_old_outgoing_bot_private_and_forwarded_are_ignored(self):
        for changes in [dict(chat_id=999), dict(timestamp=100), dict(outgoing=True), dict(is_bot=True), dict(is_group=False), dict(forwarded=True)]:
            with self.subTest(changes=changes):
                self.assertNotEqual(await self.engine.process(self.incoming(**changes)), 'sent')
        self.assertEqual(self.sent, [])

    async def test_paused_and_out_of_stock(self):
        self.store.set('active', False)
        self.assertEqual(await self.engine.process(self.incoming()), 'paused')
        self.store.set('active', True)
        self.store.replace_products(parse_catalog('iPhone 17 256GB Black SIM|85000|0'))
        self.assertEqual(await self.engine.process(self.incoming()), 'no_match')

    async def test_rate_limit(self):
        self.engine.max_per_minute = 1
        await self.engine.process(self.incoming())
        self.assertEqual(await self.engine.process(self.incoming(message_id=2, sender_id=56)), 'rate_limited')

    async def test_uncertain_send_pauses_and_is_not_retried(self):
        async def send(*args):
            raise SendFailure('timeout', uncertain=True)
        self.engine.send = send
        self.assertEqual(await self.engine.process(self.incoming()), 'uncertain')
        self.assertFalse(self.store.get('active'))
        self.store.set('active', True)
        self.assertEqual(await self.engine.process(self.incoming()), 'duplicate')

    async def test_anonymous_channel_and_missing_sender_never_receive_quotes(self):
        for sender_id in (0, -123, -1000000000123):
            with self.subTest(sender_id=sender_id):
                self.assertEqual(await self.engine.process(self.incoming(sender_id=sender_id)), 'ignored')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.recent(), [])

    async def test_recipient_only_rejection_keeps_other_buyers_enabled(self):
        attempts = []

        async def send(recipient, source_message, text):
            attempts.append((recipient, source_message))
            if recipient == 55:
                raise SendFailure('UserPrivacyRestrictedError', pause=False)
            self.sent.append((recipient, source_message, text))

        self.engine.send = send
        self.assertEqual(await self.engine.process(self.incoming()), 'failed')
        self.assertTrue(self.store.get('active'))
        delivery = self.store.delivery(-100123, 1)
        self.assertEqual((delivery['status'], delivery['detail']), ('failed', 'UserPrivacyRestrictedError'))
        # Retrying the same source event never attempts a second private send.
        self.assertEqual(await self.engine.process(self.incoming()), 'duplicate')
        self.assertEqual(attempts, [(55, 1)])
        self.assertEqual(await self.engine.process(self.incoming(sender_id=56, message_id=2)), 'sent')
        self.assertEqual(self.sent[0][:2], (56, 2))

    async def test_default_send_failure_still_pauses_all_replies(self):
        async def send(*args):
            raise SendFailure('flood_wait_30s')

        self.engine.send = send
        self.assertEqual(await self.engine.process(self.incoming()), 'failed')
        self.assertFalse(self.store.get('active'))
        self.assertEqual(self.store.delivery(-100123, 1)['status'], 'failed')

    async def test_uncertain_send_always_pauses_even_if_nonpausing_was_requested(self):
        async def send(*args):
            raise SendFailure('timeout', uncertain=True, pause=False)

        self.engine.send = send
        self.assertEqual(await self.engine.process(self.incoming()), 'uncertain')
        self.assertFalse(self.store.get('active'))
        self.assertEqual(self.store.delivery(-100123, 1)['status'], 'uncertain')

    async def test_inventory_and_dedup_survive_restart(self):
        await self.engine.process(self.incoming())
        self.store.close()
        self.store = Store(self.path)
        self.engine.store = self.store
        self.assertEqual(len(self.store.products()), 1)
        self.assertEqual(await self.engine.process(self.incoming()), 'duplicate')

    async def test_interrupted_delivery_is_marked_uncertain_on_restart(self):
        self.store.reserve(-100123, 9, 55, 'iPhone 17', 120, now=1000)
        self.store.close()
        self.store = Store(self.path)
        self.engine.store = self.store
        self.assertFalse(self.store.get('active'))
        self.assertIn('uncertain', [r['status'] for r in self.store.recent()])

    async def test_inventory_change_during_matching_cancels_send(self):
        def find(*args):
            return 'buy', parse_catalog('iPhone 17 256GB Black SIM|85000|2')
        async def work(*args):
            self.store.replace_products(parse_catalog('iPhone 18|90000|1'))
            return find()
        with patch('catalog_bot.engine.asyncio.to_thread', work):
            self.assertEqual(await self.engine.process(self.incoming()), 'cancelled')
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
