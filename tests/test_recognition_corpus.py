import json
import tempfile
import unittest
from pathlib import Path

from catalog_bot.recognition_check import load_corpus, run_checks


class RecognitionCorpusTests(unittest.TestCase):
    def write_corpus(self, data):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'cases.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def sample(self):
        return {
            'schema_version': 1,
            'metadata': {'catalog_kind': 'synthetic_test_only'},
            'products': [
                {'sku': 'APPLE', 'name': 'Apple iPhone 17 256GB Black SIM', 'price': '1000'},
                {'sku': 'XIAOMI', 'name': 'Xiaomi 17 12/256GB Black SIM', 'price': '500'},
            ],
            'cases': [{'id': 'brand', 'category': 'brand', 'query': 'iPhone17 256 Black SIM',
                       'expected': ['APPLE'], 'classification': 'bare'}],
        }

    def test_independent_corpus_matches_expected_products(self):
        result = run_checks()
        self.assertEqual(result.passed, result.total, '\n'.join(
            f'{f.case_id}: {f.query!r}: expected={f.expected}, actual={f.actual}' for f in result.failures[:30]))
        self.assertGreaterEqual(result.sources['handwritten'].total, 197)
        self.assertGreaterEqual(result.sources['generated'].total, 1218)
        self.assertGreater(result.categories['negative-brand'].total, 0)

    def test_competitor_catalog_and_classification_are_checked(self):
        data = self.sample()
        result = run_checks(self.write_corpus(data))
        self.assertEqual((result.total, result.passed), (1, 1))
        data['cases'][0]['expected'] = ['XIAOMI']
        data['cases'][0]['classification'] = 'buy'
        result = run_checks(self.write_corpus(data))
        self.assertEqual((result.total, result.passed), (1, 0))
        failure = result.failures[0]
        self.assertEqual(failure.expected, ('XIAOMI',))
        self.assertEqual(failure.actual, ('APPLE',))
        self.assertEqual(failure.expected_classification, 'buy')
        self.assertEqual(failure.actual_classification, 'bare')

    def test_explicit_catalog_context_does_not_create_brand_fallback(self):
        data = self.sample()
        data['cases'] = [{'id': 'apple-absent', 'category': 'negative-brand',
                          'query': 'iPhone17 256 Black SIM', 'expected': [],
                          'catalog_skus': ['XIAOMI']}]
        self.assertEqual(run_checks(self.write_corpus(data)).passed, 1)
        data['cases'][0]['expected'] = ['APPLE']
        with self.assertRaisesRegex(ValueError, 'expected'):
            load_corpus(self.write_corpus(data))

    def test_generated_cases_are_deterministic_and_deduplicate_within_group(self):
        data = self.sample()
        data['generated_groups'] = [{'id': 'formats', 'category': 'brand', 'expected': ['APPLE'],
                                     'parts': [['', 'Куплю ', 'Куплю '], ['iPhone17 256 Black SIM']]}]
        path = self.write_corpus(data)
        products, first = load_corpus(path)
        _, second = load_corpus(path)
        self.assertEqual(len(products), 2)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        self.assertEqual([case.source for case in first], ['handwritten', 'generated', 'generated'])
        self.assertEqual(run_checks(path).passed, 3)

    def test_invalid_oracle_cannot_silently_pass(self):
        for mutation in ('unknown-sku', 'duplicate-sku', 'duplicate-id', 'bad-classification', 'bad-stock'):
            data = self.sample()
            if mutation == 'unknown-sku':
                data['cases'][0]['expected'] = ['MISSING']
            elif mutation == 'duplicate-sku':
                data['products'].append(data['products'][0].copy())
            elif mutation == 'duplicate-id':
                data['cases'].append(data['cases'][0].copy())
            elif mutation == 'bad-classification':
                data['cases'][0]['classification'] = 'guessed'
            else:
                data['products'][0]['stock'] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                run_checks(self.write_corpus(data))

    def test_unbounded_generation_is_rejected(self):
        data = self.sample()
        data['generated_groups'] = [{'id': 'huge', 'category': 'brand', 'expected': ['APPLE'],
                                     'parts': [['x'] * 1000, ['y'] * 1000]}]
        with self.assertRaisesRegex(ValueError, 'слишком много'):
            load_corpus(self.write_corpus(data))
