import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from catalog_bot.account_roles import prepare_account_roles
from catalog_bot.prices import parse_price_feed
from catalog_bot.pricing import MarkupPolicy, MarkupRule
from catalog_bot.storage import Store


class AccountRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'data'
        self.directory.mkdir(mode=0o755)
        self.store = Store(self.directory / 'catalog.sqlite3')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_existing_account_becomes_price_only_without_touching_session_or_catalog(self):
        session_path = self.directory / 'account.session'
        session_bytes = b'opaque synthetic session bytes\x00\x01\xff'
        session_path.write_bytes(session_bytes)
        self.store.set('telegram_account_id', 77)
        self.store.set('telegram_account_name', 'supplier-account')
        self.store.toggle_chat(-100123, 'Закупки')
        self.store.toggle_chat(-100456, 'Тест')
        selected = self.store.chats()
        self.store.set('group_directory', {'account_id': 77, 'items': list(selected.items()), 'complete': True})
        feed = parse_price_feed('• Galaxy S25 12/256 Navy🇮🇳\n🇮🇳 От 1 шт - 49 000\nВсего найдено 1 товаров')
        self.store.apply_price_snapshot(feed, 'bot:123')
        self.store.set_markup_policy(MarkupPolicy(MarkupRule('fixed', Decimal('1000'))))
        self.store.set('price_auto_interval', 1800)
        self.store.set('price_auto_enabled', False)
        self.store.set('active', True)
        protected_tables = ('products', 'price_runs', 'price_changes')
        before = {table: [tuple(r) for r in self.store.db.execute(f'SELECT * FROM {table}')]
                  for table in protected_tables}
        protected_settings = {key: self.store.get(key) for key in
                              ('price_source', 'price_ready', 'price_updated_at', 'catalog_version',
                               'markup_rules', 'pricing_version', 'price_auto_interval', 'price_auto_enabled')}
        # The helper has no reason to open, read, rename, delete or copy a login.
        with (patch.object(Path, 'open', side_effect=AssertionError('session must remain opaque')),
              patch.object(Path, 'rename', side_effect=AssertionError('session must not move'))):
            reply_dir, price_dir = prepare_account_roles(self.store, self.directory)
        self.assertEqual((reply_dir, price_dir), (self.directory / 'replies', self.directory))
        self.assertEqual(session_path.read_bytes(), session_bytes)
        self.assertFalse((reply_dir / 'account.session').exists())
        self.assertEqual(reply_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(price_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.get('price_account_id'), 77)
        self.assertEqual(self.store.get('price_account_name'), 'supplier-account')
        self.assertIsNone(self.store.get('telegram_account_id'))
        self.assertIsNone(self.store.get('telegram_account_name'))
        self.assertFalse(self.store.get('active'))
        self.assertEqual(self.store.chats(), {})
        self.assertEqual(self.store.get('group_directory'), {})
        self.assertEqual(self.store.get('legacy_selected_chats'), [list(row) for row in selected.items()])
        self.assertTrue(self.store.get('role_split_initialized'))
        for table in protected_tables:
            self.assertEqual([tuple(r) for r in self.store.db.execute(f'SELECT * FROM {table}')], before[table])
        for key, value in protected_settings.items():
            self.assertEqual(self.store.get(key), value, key)

    def test_second_prepare_preserves_new_account_selected_groups_and_active_mode(self):
        self.store.set('telegram_account_id', 77)
        self.store.set('telegram_account_name', 'old')
        self.store.toggle_chat(-100123, 'Старая группа')
        prepare_account_roles(self.store, self.directory)
        self.store.set('telegram_account_id', 88)
        self.store.set('telegram_account_name', 'new seller')
        self.store.toggle_chat(-100999, 'Новая группа')
        cache = {'account_id': 88, 'items': [[-100999, 'Новая группа']], 'complete': True}
        self.store.set('group_directory', cache)
        self.store.set('active', True)
        before = [tuple(row) for row in self.store.db.execute('SELECT * FROM settings ORDER BY key')]
        self.store.close()
        self.store = Store(self.directory / 'catalog.sqlite3')
        prepare_account_roles(self.store, self.directory)
        self.assertEqual([tuple(row) for row in self.store.db.execute('SELECT * FROM settings ORDER BY key')], before)
        self.assertEqual(self.store.get('telegram_account_id'), 88)
        self.assertEqual(self.store.get('price_account_id'), 77)
        self.assertEqual(self.store.chats(), {-100999: 'Новая группа'})
        self.assertEqual(self.store.get('group_directory'), cache)
        self.assertTrue(self.store.get('active'))
        self.assertEqual(self.store.get('legacy_selected_chats'), [[-100123, 'Старая группа']])

    def test_fresh_install_does_not_create_a_price_identity_or_session(self):
        reply_dir, price_dir = prepare_account_roles(self.store, str(self.directory))
        self.assertIsNone(self.store.get('price_account_id'))
        self.assertIsNone(self.store.get('price_account_name'))
        self.assertEqual(self.store.get('legacy_selected_chats'), [])
        self.assertFalse((price_dir / 'account.session').exists())
        self.assertFalse((reply_dir / 'account.session').exists())

    def test_failed_migration_rolls_back_settings_chat_backup_and_clearing(self):
        self.store.set('telegram_account_id', 77)
        self.store.set('telegram_account_name', 'old')
        self.store.set('active', True)
        self.store.toggle_chat(-100123, 'Старая группа')
        before = [tuple(row) for row in self.store.db.execute('SELECT * FROM settings ORDER BY key')]
        original_set = self.store.set
        def fail_set(key, value):
            if key == 'active':
                raise RuntimeError('synthetic migration failure')
            original_set(key, value)
        with patch.object(self.store, 'set', side_effect=fail_set):
            with self.assertRaises(RuntimeError):
                prepare_account_roles(self.store, self.directory)
        self.assertEqual([tuple(row) for row in self.store.db.execute('SELECT * FROM settings ORDER BY key')], before)
        self.assertEqual(self.store.chats(), {-100123: 'Старая группа'})
        self.assertFalse(self.store.get('role_split_initialized', False))
        prepare_account_roles(self.store, self.directory)
        self.assertTrue(self.store.get('role_split_initialized'))


if __name__ == '__main__':
    unittest.main()
