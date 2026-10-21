import json
import tempfile
import unittest
from pathlib import Path

from catalog_bot.lexicon import DATA_PATH, _load, normalize_text, normalize_with_notes


class RecognitionLexiconTests(unittest.TestCase):
    def test_joined_names_and_versions_have_same_identity(self):
        self.assertEqual(normalize_text('айфон17промакс 256 ГБ черного 1сим'),
                         'iphone 17 pro max 256gb black 1 sim')
        self.assertEqual(normalize_text('17PM 1 ТБ glacier e-sim'),
                         '17 pro max 1tb glacier esim')

    def test_model_numbers_suffixes_and_supplier_codes_are_preserved(self):
        for spelling, expected in (
                ('OnePlus15R', 'oneplus 15r'), ('OnePlus15X', 'oneplus 15x'),
                ('RW4012-MAX', 'rw4012-max'), ('IP17-256-BK-SIM', 'ip17-256-bk-sim'),
                ('IP17-REFURB', 'ip17-refurb'), ('IP17-черный-SIM', 'ip17-черный-sim'),
                ('SM-S942B/DS', 'sm-s942b/ds'),
                ('C26', 'c26'), ('S25', 's25')):
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize_text(spelling), expected)

    def test_cyrillic_samsung_series_requires_brand_context(self):
        self.assertEqual(normalize_text('сасунг с26 ультра 512 черный'), 'samsung s26 ultra 512 black')
        self.assertEqual(normalize_text('С26'), 'с26')
        self.assertEqual(normalize_text('POCO C26'), 'poco c26')
        self.assertEqual(normalize_text('самсунг а57 12/512'), 'samsung a57 12/512')

    def test_joined_brand_and_alpha_model_preserve_suffixes(self):
        for source, expected in (
                ('SamsungGalaxyS26Ultra', 'samsung galaxy s26 ultra'),
                ('GalaxyS26Ultra', 'galaxy s26 ultra'), ('VivoX300', 'vivo x300'),
                ('POCOF7', 'poco f7'), ('RealmeGT8', 'realme gt 8'),
                ('SonyWH1000XM6', 'sony wh1000xm6'), ('OnePlusR15', 'oneplus r15')):
            with self.subTest(source=source):
                self.assertEqual(normalize_text(source), expected)

    def test_units_are_contextual_and_do_not_change_amounts(self):
        self.assertEqual(normalize_text('512гигов 1терабайт 47 мм'), '512gb 1tb 47mm')
        self.assertEqual(normalize_text('Samsung S26 Ultra 512GB BK'), 'samsung s26 ultra 512gb black')
        self.assertEqual(normalize_text('RX512-ГБ-BK'), 'rx512-гб-bk')

    def test_typo_correction_is_limited_to_brand_words(self):
        normalized, notes = normalize_with_notes('samusng S26 ultra 512 Blackish')
        self.assertEqual(normalized, 'samsung s26 ultra 512 blackish')
        self.assertTrue(any('samusng' in note for note in notes))
        self.assertEqual(normalize_text('SAMUSNG-CODE'), 'samusng-code')
        self.assertEqual(normalize_text('SAMUSNG26'), 'samusng26')
        self.assertEqual(normalize_text('samusng'), 'samusng')
        self.assertEqual(normalize_text('S26ulrta 512'), 's26ulrta 512')

    def test_normalization_keeps_negations_structure_and_meaningful_symbols(self):
        text = 'Куплю айфон17 256 не черный SIM+eSIM\nOnePlus15R 12/512 Mint Breeze -59'
        result = normalize_text(text)
        self.assertIn('\n', result)
        self.assertIn('не black', result)
        self.assertIn('sim+esim', result)
        self.assertIn('12/512', result)
        self.assertIn('-59', result)
        self.assertIn('15r', result)

    def test_distinct_shades_are_not_guessed_as_base_colors(self):
        self.assertEqual(normalize_text('темно-синий мятный обсидиан'), 'navy mint obsidian')
        self.assertNotEqual(normalize_text('navy'), normalize_text('blue'))
        self.assertNotEqual(normalize_text('obsidian'), normalize_text('black'))
        self.assertNotEqual(normalize_text('mint'), normalize_text('green'))

    def test_invalid_dictionary_fails_before_guessing(self):
        data = json.loads(DATA_PATH.read_text())
        del data['brand_owners']['iphone']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'aliases.json'
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'brand_owners'):
                _load(path)
