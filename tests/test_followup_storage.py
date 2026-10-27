import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from catalog_bot.catalog import Product
from catalog_bot.storage import Store


class FollowupStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.store = Store(self.path)
        self.products = [
            Product('Galaxy S25 12/256 Navy', Decimal('49000.50'), None, 'S25-NAVY', '🇮🇳', '🇮🇳'),
            Product('Galaxy S25 12/256 Silver', Decimal('49500'), 3, 'S25-SILVER', '🇪🇺', '🇪🇺'),
        ]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def save(self, account=1, buyer=55, source=-100123, message=7, now=1000, products=None, sent_ids=(101, 102)):
        return self.store.save_quote(account, buyer, source, message, 'Samsung S25 256',
                                     products or self.products, now=now, sent_message_ids=sent_ids)

    def test_quote_snapshot_preserves_decimal_unknown_stock_and_all_product_fields(self):
        product = Product('Товар', Decimal('12345.6780'), None, 'SKU', '🇨🇳', '🇬🇧', 5, ('warning',))
        quote_id = self.save(products=[product])
        quote = self.store.latest_quote(1, 55, now=1001)
        self.assertEqual(quote['quote_id'], quote_id)
        self.assertEqual(quote['products'], [product])
        self.assertIsInstance(quote['products'][0].price, Decimal)
        self.assertEqual(str(quote['products'][0].price), '12345.6780')
        self.assertEqual(quote['sent_message_ids'], [101, 102])
        self.assertEqual(quote['request_text'], 'Samsung S25 256')
        self.store.replace_products([replace(product, price=Decimal('99999'))])
        self.assertEqual(self.store.latest_quote(1, 55, now=1002)['products'], [product])

    def test_missing_price_is_distinct_from_zero_and_survives_restart(self):
        products = [replace(self.products[0], price=None), replace(self.products[1], price=Decimal('0'))]
        self.save(products=products)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.latest_quote(1, 55, now=1001)['products'], products)

    def test_latest_quote_and_reply_to_are_strictly_isolated_by_account_and_buyer(self):
        first = self.save()
        other_account = self.save(account=2)
        other_buyer = self.save(buyer=66, message=8)
        second = self.save(message=9, now=1001, sent_ids=(103,))
        self.assertEqual(self.store.latest_quote(1, 55, now=1002)['quote_id'], second)
        self.assertEqual(self.store.quote_for_reply(1, 55, 101, now=1002)['quote_id'], first)
        self.assertEqual(self.store.quote_for_reply(2, 55, 101, now=1002)['quote_id'], other_account)
        self.assertEqual(self.store.quote_for_reply(1, 66, 101, now=1002)['quote_id'], other_buyer)
        self.assertIsNone(self.store.quote_for_reply(1, 55, 99999, now=1002))
        self.assertIsNone(self.store.latest_quote(3, 55, now=1002))
        self.assertIsNone(self.store.latest_quote(1, 77, now=1002))

    def test_quote_ttl_is_at_most_24_hours_and_reply_does_not_fall_back(self):
        self.save(now=1000)
        self.assertIsNotNone(self.store.latest_quote(1, 55, now=1000 + 86400))
        self.assertIsNone(self.store.latest_quote(1, 55, now=1000 + 86401))
        self.assertIsNone(self.store.latest_quote(1, 55, now=1000 + 86401, max_age=999999))
        self.assertIsNone(self.store.quote_for_reply(1, 55, 101, now=1000 + 86401))
        self.assertIsNone(self.store.latest_quote(1, 55, now=999))
        self.assertIsNone(self.store.latest_quote(1, 55, now=1010, max_age=5))

    def test_repeated_save_keeps_first_snapshot_time_and_adds_sent_message_mapping(self):
        first = self.save()
        second = self.save(now=1100, products=[replace(self.products[0], price=Decimal('99999'))], sent_ids=(103,))
        self.assertEqual(first, second)
        quote = self.store.latest_quote(1, 55, now=1100)
        self.assertEqual(quote['products'], self.products)
        self.assertEqual(quote['created'], 1000)
        self.assertEqual(quote['sent_message_ids'], [101, 102, 103])
        with self.assertRaises(ValueError):
            self.save(buyer=66)
        self.assertIsNone(self.store.latest_quote(1, 66, now=1100))

    def test_followup_is_reserved_once_before_send_and_completion_is_final(self):
        quote_id = self.save()
        self.assertTrue(self.store.reserve_followup(1, 55, 200, quote_id, 'discount', now=1001))
        self.assertEqual(self.store.followup(1, 55, 200)['status'], 'pending')
        self.assertFalse(self.store.reserve_followup(1, 55, 200, quote_id, 'hold', now=1001))
        self.assertTrue(self.store.finish_followup(1, 55, 200, 'sent'))
        self.assertFalse(self.store.finish_followup(1, 55, 200, 'uncertain'))
        self.assertEqual(self.store.followup(1, 55, 200)['status'], 'sent')
        self.assertFalse(self.store.reserve_followup(1, 55, 200, quote_id, 'discount', now=1002))

    def test_followup_cannot_reference_another_buyer_account_or_expired_quote(self):
        quote_id = self.save()
        self.assertFalse(self.store.reserve_followup(2, 55, 200, quote_id, 'hold', now=1001))
        self.assertFalse(self.store.reserve_followup(1, 66, 200, quote_id, 'hold', now=1001))
        self.assertFalse(self.store.reserve_followup(1, 55, 200, quote_id, 'hold', now=1000 + 86401))
        self.assertFalse(self.store.reserve_followup(1, 55, 200, 99999, 'hold', now=1001))
        with self.assertRaises(ValueError):
            self.store.reserve_followup(1, 55, 200, quote_id, 'unsupported', now=1001)
        self.assertIsNone(self.store.followup(1, 55, 200))

    def test_followup_message_ids_are_scoped_to_account_and_buyer(self):
        quote1 = self.save()
        quote2 = self.save(account=2)
        quote3 = self.save(buyer=66, message=8)
        for account, buyer, quote in ((1, 55, quote1), (2, 55, quote2), (1, 66, quote3)):
            self.assertTrue(self.store.reserve_followup(account, buyer, 200, quote, 'clarify', now=1001))
        self.store.finish_followup(2, 55, 200, 'failed', 'x' * 200)
        self.assertEqual(self.store.followup(1, 55, 200)['status'], 'pending')
        self.assertEqual(self.store.followup(2, 55, 200)['status'], 'failed')
        self.assertEqual(len(self.store.followup(2, 55, 200)['detail']), 120)

    def test_latest_followup_tracks_clarification_and_is_scoped_to_quote_and_buyer(self):
        quote_id = self.save()
        other_quote = self.save(message=8, now=1001, sent_ids=(103,))
        self.assertIsNone(self.store.latest_followup(1, 55, quote_id))
        self.store.reserve_followup(1, 55, 200, quote_id, 'clarify', now=1002)
        self.store.finish_followup(1, 55, 200, 'sent')
        latest = self.store.latest_followup(1, 55, quote_id)
        self.assertEqual((latest['kind'], latest['status']), ('clarify', 'sent'))
        self.assertIsNone(self.store.latest_followup(2, 55, quote_id))
        self.assertIsNone(self.store.latest_followup(1, 66, quote_id))
        self.assertIsNone(self.store.latest_followup(1, 55, other_quote))
        self.store.reserve_followup(1, 55, 201, quote_id, 'hold', now=1002)
        self.assertEqual(self.store.latest_followup(1, 55, quote_id)['kind'], 'hold')

    def test_restart_marks_pending_followups_uncertain_and_never_retries_them(self):
        quote_id = self.save()
        self.store.set('active', True)
        self.store.reserve_followup(1, 55, 200, quote_id, 'discount', now=1001)
        self.store.reserve_followup(1, 55, 201, quote_id, 'clarify', now=1001)
        self.store.finish_followup(1, 55, 201, 'sent')
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.get('active'))
        self.assertEqual(self.store.followup(1, 55, 200)['status'], 'uncertain')
        self.assertEqual(self.store.followup(1, 55, 200)['detail'], 'process_restart')
        self.assertEqual(self.store.followup(1, 55, 201)['status'], 'sent')
        self.assertFalse(self.store.reserve_followup(1, 55, 200, quote_id, 'discount', now=1002))

    def test_hold_reserves_each_quoted_product_only_once_without_changing_stock(self):
        self.store.replace_products(self.products)
        quote_id = self.save()
        first, second = self.products
        self.assertEqual(self.store.reserve_hold(1, 55, quote_id, [first, first], now=1001), [first])
        self.assertEqual(self.store.reserve_hold(1, 55, quote_id, [first], now=1001), [])
        self.assertEqual(self.store.finish_hold(1, 55, quote_id, [first], 'sent'), 1)
        self.assertEqual(self.store.reserve_hold(1, 55, quote_id, [first, second], now=1002), [second])
        self.assertEqual(self.store.products(), self.products)
        self.assertEqual([r['status'] for r in self.store.holds(1, 55, quote_id)], ['sent', 'pending'])

    def test_hold_rejects_unquoted_or_modified_products_and_wrong_context(self):
        quote_id = self.save()
        first = self.products[0]
        self.assertEqual(self.store.reserve_hold(1, 55, quote_id, [replace(first, price=Decimal('1'))], now=1001), [])
        self.assertEqual(self.store.reserve_hold(1, 55, quote_id, [Product('iPhone 17')], now=1001), [])
        self.assertEqual(self.store.reserve_hold(2, 55, quote_id, [first], now=1001), [])
        self.assertEqual(self.store.reserve_hold(1, 66, quote_id, [first], now=1001), [])
        self.assertEqual(self.store.reserve_hold(1, 55, quote_id, [first], now=1000 + 86401), [])
        self.assertEqual(self.store.holds(1, 55, quote_id), [])

    def test_restart_marks_hold_uncertain_and_repeated_hold_never_notifies_again(self):
        quote_id = self.save()
        first = self.products[0]
        self.store.reserve_hold(1, 55, quote_id, [first], now=1001)
        self.store.set('active', True)
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.get('active'))
        self.assertEqual(self.store.holds(1, 55, quote_id)[0]['status'], 'uncertain')
        self.assertEqual(self.store.reserve_hold(1, 55, quote_id, [first], now=1002), [])
        self.assertEqual(self.store.finish_hold(1, 55, quote_id, [first], 'sent'), 0)

    def test_same_product_can_be_held_by_different_buyers_and_accounts(self):
        first = self.products[0]
        for account, buyer, message in ((1, 55, 7), (2, 55, 7), (1, 66, 8)):
            quote_id = self.save(account=account, buyer=buyer, message=message)
            self.assertEqual(self.store.reserve_hold(account, buyer, quote_id, [first], now=1001), [first])

    def test_same_name_offers_with_different_sku_or_price_can_be_held_independently(self):
        first = self.products[0]
        second = replace(first, sku='ANOTHER-SKU')
        third = replace(first, price=Decimal('49999'))
        quote_id = self.save(products=[first, second, third])
        for product in (first, second, third):
            self.assertEqual(self.store.reserve_hold(1, 55, quote_id, [product], now=1001), [product])
            self.assertEqual(self.store.finish_hold(1, 55, quote_id, [product], 'sent'), 1)
        self.assertEqual(self.store.reserve_hold(1, 55, quote_id, [first, second, third], now=1002), [])
        self.assertEqual(len(self.store.holds(1, 55, quote_id)), 3)

    def test_quote_save_prunes_expired_context_using_its_clock_without_restart(self):
        first = self.save(now=1000)
        second = self.save(message=8, now=1000 + 86401, sent_ids=(103,))
        self.assertGreater(second, first)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM quotes').fetchone()[0], 1)
        self.assertIsNone(self.store.quote_for_reply(1, 55, 101, now=1000 + 86401))
        self.assertEqual(self.store.latest_quote(1, 55, now=1000 + 86401)['quote_id'], second)

    def test_sent_followups_share_the_quote_delivery_rate_limit(self):
        quote_id = self.save()
        self.store.reserve(-100123, 9, 55, 's25 navy', 120, now=1001)
        self.store.finish(-100123, 9, 'sent')
        self.store.reserve_followup(1, 55, 200, quote_id, 'discount', now=1002)
        self.store.finish_followup(1, 55, 200, 'sent')
        self.store.reserve_followup(1, 55, 201, quote_id, 'hold', now=1002)
        self.store.finish_followup(1, 55, 201, 'failed')
        self.assertEqual(self.store.sent_since(1000), 2)
        self.assertEqual(self.store.sent_since(1001), 1)
        self.assertEqual(self.store.sent_since(1002), 0)

    def test_pruning_expired_quote_preserves_followup_replay_protection(self):
        quote_id = self.save()
        self.store.reserve_followup(1, 55, 200, quote_id, 'discount', now=1000 + 86390)
        self.store.finish_followup(1, 55, 200, 'sent')
        with patch('catalog_bot.storage.time.time', return_value=1000 + 86401):
            self.store.prune()
        self.assertIsNone(self.store.latest_quote(1, 55, now=1000 + 86401))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM quote_messages').fetchone()[0], 0)
        newer = self.save(message=8, now=1000 + 86401, sent_ids=(103,))
        self.assertFalse(self.store.reserve_followup(1, 55, 200, newer, 'discount', now=1000 + 86402))


if __name__ == '__main__':
    unittest.main()
