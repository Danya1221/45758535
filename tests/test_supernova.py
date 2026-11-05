import io
import unittest
from decimal import Decimal

from openpyxl import Workbook

from catalog_bot.prices import PriceFeedError
from catalog_bot.supernova import parse_supernova_xlsx


def workbook_bytes(rows):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Прайс'
    sheet.append(['ИТОГО ПО ЗАКАЗУ'])
    sheet.append(['Наименование', 'Re.order', 'Кол-во', 'Цена от 1 шт', 'Цена от 5 шт', 'Цена от 10 шт'])
    for row in rows:
        sheet.append(row)
    sheet.append(['Файл выгружен для: пользователь · Прайс именной'])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


class SupernovaTest(unittest.TestCase):
    def test_price_stock_region_and_export_total(self):
        snapshot = parse_supernova_xlsx(workbook_bytes([
            ['Product One 🇪🇺', 'code', '20+', 1000, 900, 800],
            ['Product Two 🇭🇰', None, 3, 2000, 1900, 1800],
        ]))
        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.raw_count, 2)
        first, second = snapshot.products
        self.assertEqual((first.name, first.country, first.stock, first.price), ('Product One', '🇪🇺', 20, Decimal('1000')))
        self.assertEqual((second.country, second.stock, second.price), ('🇭🇰', 3, Decimal('2000')))

    def test_incomplete_rows_are_rejected(self):
        for row in [ ['Product One', None, None, 1000], ['Product One', None, 2, None],
                     ['Product One', None, 'много', 1000], ['Product One', None, 2, 0] ]:
            with self.subTest(row=row), self.assertRaises(PriceFeedError):
                parse_supernova_xlsx(workbook_bytes([row]))

    def test_duplicate_is_not_silently_overwritten(self):
        with self.assertRaises(PriceFeedError):
            parse_supernova_xlsx(workbook_bytes([['Product One', None, 2, 1000], ['Product One', None, 3, 1200]]))

    def test_bad_file(self):
        with self.assertRaises(PriceFeedError):
            parse_supernova_xlsx(b'not an xlsx')
