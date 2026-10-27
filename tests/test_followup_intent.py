import unittest
from decimal import Decimal

from catalog_bot.catalog import Product
from catalog_bot.followup_intent import classify_followup, resolve_hold, strip_hold_request


class FollowupIntentTests(unittest.TestCase):
    def test_explicit_discount_requests(self):
        for text in ('-200', '−200', '- 200 ₽', 'минус200', 'минус 200 рублей',
                     'скинь 200', 'скинь200', 'скиньте 200', 'уступишь 200?', 'уступишь200', 'уступите 200',
                     'сделай скидку', 'можно скидку?', 'торг', 'торг есть?',
                     'а -200 можно?', 'можно подешевле?', 'снизьте цену'):
            with self.subTest(text=text):
                self.assertEqual(classify_followup(text), 'discount')

    def test_hold_requests(self):
        for text in ('отложите', 'Отложи, пожалуйста!', 'забронируй', 'забронируйте',
                     'бронь', 'бронь до вечера', 'поставьте в резерв', 'можно забронировать?',
                     'отложите Samsung S25 Ultra 512 черный', 'зарезервируйте'):
            with self.subTest(text=text):
                self.assertEqual(classify_followup(text), 'hold')

    def test_discount_precedes_conditional_or_combined_hold(self):
        for text in ('отложите, если скинете 200', 'бронь и скидку 200',
                     'забронируй -200', '−200, отложи', 'отложи, уступишь 200?'):
            with self.subTest(text=text):
                self.assertEqual(classify_followup(text), 'discount')

    def test_numbers_identifiers_and_ordinary_replies_are_not_followups(self):
        for text in ('SM-S938B', 'ABC-200', '100-200', '100 - 200', '2026-10-26',
                     '26.10.2026', '12/512', 'спасибо', 'ок', 'нет', 'черный 512', '',
                     'Samsung S25 Ultra 512 Black', 'код SM-S938B/DS'):
            with self.subTest(text=text):
                self.assertIsNone(classify_followup(text))

    def test_negative_and_cancelled_intents_are_not_accepted(self):
        for text in ('нет скидки', 'без скидки', 'скидка не нужна', 'торг не нужен',
                     'не скидывай', 'не уступайте', 'не откладывайте', 'не надо откладывать',
                     'не нужно бронировать', 'бронь не нужна', 'бронь, не нужна',
                     'бронь пока не нужна', 'не ставь бронь', 'без брони', 'скидку не делайте',
                     'снимите бронь', 'отмена брони', 'уберите резерв', 'бронь отменяю',
                     'у меня бронь', 'у вас есть бронь?', 'он в резерве?', 'бронь есть?',
                     'цена со скидкой', 'скидка уже учтена'):
            with self.subTest(text=text):
                self.assertIsNone(classify_followup(text))

    def test_negative_discount_does_not_cancel_positive_hold(self):
        for text in ('отложите без скидки', 'скидка не нужна, забронируй'):
            with self.subTest(text=text):
                self.assertEqual(classify_followup(text), 'hold')

    def test_invalid_or_very_long_input_has_no_action(self):
        for text in (None, 200, 'отложите ' + 'а' * 4000):
            with self.subTest(text=text if isinstance(text, int) else type(text)):
                self.assertIsNone(classify_followup(text))


class HoldResolutionTests(unittest.TestCase):
    def setUp(self):
        self.black = Product('Galaxy S25 Ultra 12/512 Black SM-S938B/DS', Decimal('70600'), None, country='🇨🇱')
        self.white = Product('Galaxy S25 Ultra 12/512 Whitesilver SM-S938B/DS', Decimal('71700'), None, country='🇨🇱')
        self.black256 = Product('Galaxy S25 Ultra 12/256 Black SM-S938B/DS', Decimal('66500'), None, country='🇦🇪')
        self.iphone = Product('iPhone 17 512 Black', Decimal('90000'), None)

    def test_generic_hold_uses_the_only_quoted_item(self):
        for text in ('отложите', 'бронь', 'отложи его, пожалуйста', 'поставьте в резерв до вечера',
                     'отложите без скидки', 'скидка не нужна, забронируй'):
            with self.subTest(text=text):
                self.assertEqual(resolve_hold(text, [self.black]), self.black)

    def test_generic_hold_is_ambiguous_with_multiple_quoted_items(self):
        self.assertIsNone(resolve_hold('отложите', [self.black, self.white]))
        self.assertIsNone(resolve_hold('отложите', [self.black, self.black256]))
        self.assertIsNone(resolve_hold('отложите', [self.black, self.iphone]))

    def test_complete_product_request_keeps_strict_model_memory_and_color(self):
        quoted = [self.black, self.white, self.black256, self.iphone]
        self.assertEqual(resolve_hold('отложите Samsung S25 Ultra 512 Black', quoted), self.black)
        self.assertIsNone(resolve_hold('отложите Samsung S26 Ultra 512 Black', quoted))
        self.assertIsNone(resolve_hold('отложите Samsung S25 512 Black', quoted))
        self.assertIsNone(resolve_hold('отложите Xiaomi 17 512 Black', quoted))
        self.assertIsNone(resolve_hold('отложите Samsung S25 Ultra 1TB Black', quoted))

    def test_attribute_clarification_requires_same_model_and_unique_offer(self):
        quoted = [self.black, self.white, self.black256]
        self.assertEqual(resolve_hold('черный 512', quoted), self.black)
        self.assertEqual(resolve_hold('отложите черный 512', quoted), self.black)
        self.assertEqual(resolve_hold('черный 256', quoted), self.black256)
        self.assertIsNone(resolve_hold('черный', quoted))
        self.assertIsNone(resolve_hold('512', quoted))
        self.assertIsNone(resolve_hold('черный 512', [self.black, self.iphone]))

    def test_single_snapshot_does_not_override_explicit_wrong_attributes(self):
        self.assertIsNone(resolve_hold('отложите белый 512', [self.black]))
        self.assertIsNone(resolve_hold('отложите черный 256', [self.black]))
        self.assertIsNone(resolve_hold('отложите iPhone 17 512 Black', [self.black]))

    def test_different_regions_remain_ambiguous_until_selected(self):
        eu = Product(self.black.name, self.black.price, None, country='🇪🇺')
        self.assertIsNone(resolve_hold('черный 512', [self.black, eu]))
        self.assertEqual(resolve_hold('черный 512 🇪🇺', [self.black, eu]), eu)
        self.assertEqual(resolve_hold('Samsung S25 Ultra 512 Black 🇨🇱', [self.black, eu]), self.black)

    def test_discount_negative_hold_and_unavailable_items_never_resolve(self):
        for text in ('отложи если скинешь 200', '-200', 'не откладывайте',
                     'бронь не нужна', 'отмена брони', 'без брони'):
            with self.subTest(text=text):
                self.assertIsNone(resolve_hold(text, [self.black]))
        unavailable = Product(self.black.name, self.black.price, 0)
        self.assertIsNone(resolve_hold('отложите', [unavailable]))
        self.assertIsNone(resolve_hold('отложите', [self.white, unavailable]))
        self.assertIsNone(resolve_hold('отложите', []))
        self.assertIsNone(resolve_hold('', [self.black]))

    def test_strip_request_preserves_product_numbers_and_supplier_codes(self):
        self.assertEqual(strip_hold_request('Отложите мне Samsung S25 Ultra 12/512 Black SM-S938B/DS, пожалуйста'),
                         'samsung s25 ultra 12/512 black sm-s938b/ds')


if __name__ == '__main__':
    unittest.main()
