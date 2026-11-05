import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog_bot.catalog import parse_catalog
from catalog_bot.engine import Engine, Incoming
from catalog_bot.prices import parse_price_feed
from catalog_bot.storage import Store
from catalog_bot.supernova_sync import SupernovaSync
from tests.test_supernova import workbook_bytes


class Client:
    def __init__(self, data):
        self.data = data
        self.downloads = 0

    async def get_entity(self, username):
        assert username == '@supernova_bot'
        return SimpleNamespace(id=999, bot=True)

    async def download_media(self, message, file):
        self.downloads += 1
        return self.data


def event(message_id=10, sender=999, date=None):
    return SimpleNamespace(is_private=True, out=False, fwd_from=None, chat_id=sender, sender_id=sender,
                           file=SimpleNamespace(name='price.xlsx', size=1000), id=message_id,
                           date=datetime.fromtimestamp(date or time.time(), timezone.utc), message=object())


class SupernovaSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'catalog.db')
        self.store.replace_products(parse_catalog('Sony WH-1000XM6 Black|30000|4'))
        self.client = Client(workbook_bytes([['Sony WH-1000XM6 Black 🇪🇺', None, 5, 25000]]))
        self.sync = SupernovaSync(self.store, self.client)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_receive_keeps_existing_source_and_quotes_cheapest(self):
        await self.sync.receive(event())
        self.assertEqual(len(self.store.products()), 2)
        self.store.toggle_chat(-123, 'Test')
        self.store.set('active', True)
        sent = []
        async def send(buyer, source, text):
            sent.append(text)
            return []
        engine = Engine(self.store, send)
        now = time.time()
        self.assertEqual(await engine.process(Incoming(-123, 1, 100, 'Sony WH-1000XM6 Black', now)), 'sent')
        self.assertIn('25 000 ₽', sent[0])
        self.assertNotIn('30 000 ₽', sent[0])

    async def test_expiry_and_unavailable_main_are_isolated(self):
        await self.sync.receive(event())
        self.store.set('price_ready', False)
        self.assertEqual([p.source for p in self.store.quotable_products()], ['supernova'])
        with patch('catalog_bot.storage.time.time', return_value=time.time() + 601):
            self.assertEqual(self.store.quotable_products(), [])
        self.assertEqual(len(self.store._base_products()), 1)

    async def test_primary_supplier_refresh_keeps_supernova(self):
        await self.sync.receive(event())
        source = '• Sony WH-1000XM6 Silver🇪🇺\n🇪🇺 От 1 шт - 29 000\nВсего найдено 1 товаров'
        self.store.apply_price_snapshot(parse_price_feed(source), 'bot:123')
        self.assertEqual({p.name for p in self.store.products()},
                         {'Sony WH-1000XM6 Silver', 'Sony WH-1000XM6 Black'})

    async def test_new_price_account_can_use_its_own_message_numbers(self):
        self.store.set('price_account_id', 1)
        await self.sync.receive(event(message_id=100))
        self.store.set('price_account_id', 2)
        await self.sync.receive(event(message_id=5))
        self.assertEqual(self.store.get('supernova_message_id'), 5)

    async def test_wrong_sender_or_old_message_cannot_import(self):
        await self.sync.receive(event(sender=998))
        await self.sync.receive(event(date=time.time() - 180))
        self.assertEqual(self.client.downloads, 0)
        self.assertEqual(len(self.store.products()), 1)

    async def test_incomplete_new_file_cannot_remove_previous_data(self):
        await self.sync.receive(event())
        self.client.data = workbook_bytes([['Sony WH-1000XM6 Black', None, None, 24000]])
        await self.sync.receive(event(message_id=11))
        self.assertEqual(self.store.get('supernova_message_id'), 10)
        self.assertEqual(len(self.store.products()), 2)

    async def test_successful_new_file_hides_missing_but_preserves_record(self):
        await self.sync.receive(event())
        self.client.data = workbook_bytes([['Sony WH-1000XM6 Blue 🇪🇺', None, 2, 26000]])
        await self.sync.receive(event(message_id=11))
        self.assertEqual(len(self.store.products()), 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM extra_price_items').fetchone()[0], 2)
        self.assertEqual([p.name for p in self.store.products() if p.source], ['Sony WH-1000XM6 Blue'])
