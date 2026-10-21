import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class RecognitionCommandTests(unittest.TestCase):
    def run_command(self, expected):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cases.json'
            path.write_text(json.dumps({
                'schema_version': 1,
                'metadata': {'catalog_kind': 'synthetic_test_only'},
                'products': [
                    {'sku': 'APPLE', 'name': 'Apple iPhone 17 256 Black'},
                    {'sku': 'XIAOMI', 'name': 'Xiaomi 17 256 Black'},
                ],
                'cases': [{'id': 'brand-gate', 'category': 'brand',
                           'query': 'Куплю айфон17 256 черный', 'expected': expected}],
            }), encoding='utf-8')
            environment = os.environ.copy()
            for name in ('API_ID', 'API_HASH', 'BOT_TOKEN', 'OWNER_ID', 'TELEGRAM_SESSION'):
                environment.pop(name, None)
            return subprocess.run([sys.executable, '-m', 'catalog_bot', 'check-recognition', str(path)],
                                  cwd=Path(__file__).resolve().parent.parent, env=environment,
                                  text=True, encoding='utf-8', capture_output=True, timeout=30)

    def test_offline_command_accepts_correct_oracle_without_telegram_credentials(self):
        result = self.run_command(['APPLE'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1/1 сценариев прошли', result.stdout)

    def test_wrong_oracle_causes_failing_exit_and_identifiable_difference(self):
        result = self.run_command(['XIAOMI'])
        self.assertEqual(result.returncode, 1)
        self.assertIn('Ошибка brand-gate', result.stdout)
        self.assertIn('Ожидалось: XIAOMI', result.stdout)
        self.assertIn('Получено: APPLE', result.stdout)
        self.assertIn('Не прошли: 1', result.stderr)
