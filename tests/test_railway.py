import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from catalog_bot.app import Config
from catalog_bot.sessions import export_session, load_string_session


class RailwayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.env = {'API_ID': '123', 'API_HASH': 'test', 'BOT_TOKEN': 'test',
                    'OWNER_ID': '55', 'DATA_DIR': str(self.directory / 'local')}
        self.env_patch = patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.dotenv_patch = patch('catalog_bot.app.load_dotenv')
        self.dotenv_patch.start()

    def tearDown(self):
        self.dotenv_patch.stop()
        self.env_patch.stop()
        self.temp.cleanup()

    def local_session(self):
        session = SQLiteSession(str(self.directory / 'account'))
        session.set_dc(2, '149.154.167.51', 443)
        session.auth_key = AuthKey(data=bytes(range(256)))
        session.save()
        session.close()

    def test_export_round_trip_is_private_and_does_not_print_key(self):
        self.local_session()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            target = export_session(self.directory)
        self.assertEqual(output.getvalue(), '')
        text = target.read_text()
        self.assertTrue(text.startswith('TELEGRAM_SESSION='))
        value = text.strip().split('=', 1)[1]
        restored = load_string_session(value)
        self.assertEqual(restored.auth_key.key, bytes(range(256)))
        self.assertEqual((restored.dc_id, restored.server_address, restored.port), (2, '149.154.167.51', 443))
        if os.name != 'nt':
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            target.chmod(0o644)
            export_session(self.directory)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_export_requires_existing_authorized_session(self):
        with self.assertRaisesRegex(ValueError, 'login'):
            export_session(self.directory)
        self.assertFalse((self.directory / 'account.session').exists())
        session = SQLiteSession(str(self.directory / 'account'))
        session.close()
        with self.assertRaisesRegex(ValueError, 'авторизации'):
            export_session(self.directory)
        self.assertFalse((self.directory / 'railway-session.env').exists())

    def test_invalid_session_has_safe_error(self):
        for value in ('', 'secret-account-value', '1invalid-secret-value'):
            with self.assertRaises(ValueError) as error:
                load_string_session(value)
            self.assertIn('Неверный TELEGRAM_SESSION', str(error.exception))
            if value:
                self.assertNotIn(value, str(error.exception))

    def test_config_loads_secret_and_omits_it_from_repr(self):
        self.local_session()
        value = export_session(self.directory).read_text().strip().split('=', 1)[1]
        os.environ['TELEGRAM_SESSION'] = value
        config = Config.from_env()
        self.assertEqual(config.telegram_session, value)
        self.assertNotIn(value, repr(config))

    def test_config_rejects_invalid_session_before_creating_data(self):
        os.environ['TELEGRAM_SESSION'] = 'private-invalid-value'
        with self.assertRaisesRegex(ValueError, 'Неверный TELEGRAM_SESSION'):
            Config.from_env()
        self.assertFalse(Path(self.env['DATA_DIR']).exists())

    def test_railway_requires_volume(self):
        os.environ['RAILWAY_PROJECT_ID'] = 'test-project'
        with self.assertRaisesRegex(ValueError, 'Volume'):
            Config.from_env()
        self.assertFalse(Path(self.env['DATA_DIR']).exists())

    def test_volume_path_used_when_data_dir_missing_or_empty(self):
        volume = self.directory / 'volume'
        os.environ.update(RAILWAY_ENVIRONMENT_ID='test-environment', RAILWAY_VOLUME_MOUNT_PATH=str(volume))
        del os.environ['DATA_DIR']
        self.assertEqual(Config.from_env().data_dir, volume)
        os.environ['DATA_DIR'] = ''
        self.assertEqual(Config.from_env().data_dir, volume)

    def test_explicit_path_must_stay_inside_volume(self):
        volume = self.directory / 'volume'
        os.environ.update(RAILWAY_PROJECT_ID='test-project', RAILWAY_VOLUME_MOUNT_PATH=str(volume))
        for outside in (self.directory / 'other', self.directory / 'volume-sibling'):
            os.environ['DATA_DIR'] = str(outside)
            with self.assertRaisesRegex(ValueError, 'внутри Railway Volume'):
                Config.from_env()
            self.assertFalse(outside.exists())
        os.environ['DATA_DIR'] = str(volume / 'bot')
        self.assertEqual(Config.from_env().data_dir, volume / 'bot')

    def test_local_login_does_not_require_railway_volume_or_bot_token(self):
        os.environ['RAILWAY_PROJECT_ID'] = 'test-project'
        del os.environ['BOT_TOKEN']
        del os.environ['OWNER_ID']
        config = Config.from_env(login_only=True)
        self.assertEqual(config.owner_id, 0)
        self.assertTrue(config.data_dir.is_dir())

    def test_cli_exports_to_file_without_printing_key(self):
        from catalog_bot.__main__ import main
        self.local_session()
        os.environ['DATA_DIR'] = str(self.directory)
        output = io.StringIO()
        with patch('sys.argv', ['catalog_bot', 'session-env']), contextlib.redirect_stdout(output):
            main()
        value = (self.directory / 'railway-session.env').read_text().strip().split('=', 1)[1]
        self.assertIn('railway-session.env', output.getvalue())
        self.assertNotIn(value, output.getvalue())


if __name__ == '__main__':
    unittest.main()
