import unittest

from catalog_bot.message_chunks import split_messages


def utf16_units(text):
    return len(text.encode('utf-16-le', errors='surrogatepass')) // 2


class MessageChunkTests(unittest.TestCase):
    def assert_lossless(self, text, limit):
        parts = split_messages(text, limit)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(part and utf16_units(part) <= limit for part in parts))
        return parts

    def test_empty_text_and_exact_limit(self):
        self.assertEqual(split_messages(''), [])
        self.assertEqual(split_messages('Цена: 123'), ['Цена: 123'])
        self.assertEqual(self.assert_lossless('abcd', 4), ['abcd'])

    def test_split_prefers_complete_lines_and_preserves_their_newlines(self):
        self.assertEqual(self.assert_lossless('a\nbbbb\ncc', 7), ['a\nbbbb\n', 'cc'])
        self.assertEqual(self.assert_lossless('aa\nbbbb', 5), ['aa\n', 'bbbb'])

    def test_spaces_blank_lines_and_line_endings_are_not_stripped(self):
        self.assert_lossless('  \n\n товар \r\n цена \n\t \n', 5)
        self.assertEqual(self.assert_lossless('     ', 2), ['  ', '  ', ' '])
        self.assertEqual(self.assert_lossless('\n\n\n\n\n', 2), ['\n\n', '\n\n', '\n'])

    def test_astral_emojis_count_as_two_units_and_are_indivisible(self):
        self.assertEqual(self.assert_lossless('😀😀😀', 4), ['😀😀', '😀'])
        self.assertEqual(self.assert_lossless('a😀b😀c', 3), ['a😀', 'b😀', 'c'])
        self.assertEqual(self.assert_lossless('🇷🇺\nЦена', 5), ['🇷🇺\n', 'Цена'])
        self.assertEqual(self.assert_lossless('😀a😀', 2), ['😀', 'a', '😀'])

    def test_explicit_surrogate_pair_is_not_split_either(self):
        pair = '\ud83d\ude00'
        self.assertEqual(self.assert_lossless('a' + pair + 'b', 2), ['a', pair, 'b'])

    def test_long_single_line_is_split_at_codepoint_boundaries(self):
        text = 'iPhone 17 😀 256 GB ' * 1000
        parts = self.assert_lossless(text, 4000)
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(utf16_units(part) >= 3999 for part in parts[:-1]))

    def test_many_product_rows_preserve_all_offers(self):
        text = '\n'.join(f'• Galaxy S26 512 Black 🇨🇱 — {90000 + index} ₽' for index in range(425))
        parts = self.assert_lossless(text, 4000)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(part.endswith('\n') for part in parts[:-1]))
        self.assertEqual(''.join(parts).count('• Galaxy'), 425)

    def test_invalid_limits_are_rejected_even_for_empty_text(self):
        for limit in (-1, 0, 1, True, False, 2.5, '4000', None):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    split_messages('', limit)


if __name__ == '__main__':
    unittest.main()
