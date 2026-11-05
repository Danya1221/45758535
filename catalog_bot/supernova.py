"""Read Supernova's exported spreadsheet without modifying the catalog."""
from __future__ import annotations

import io
import re
import zipfile
from decimal import Decimal

from openpyxl import load_workbook

from .catalog import CatalogError, Product, parse_price, product_key
from .prices import FLAGS, PriceFeedError, PriceSnapshot

MAX_FILE = 5_000_000
MAX_UNCOMPRESSED = 20_000_000


def parse_supernova_xlsx(data: bytes) -> PriceSnapshot:
    if not data or len(data) > MAX_FILE:
        raise PriceFeedError('Файл пуст или больше 5 МБ')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > MAX_UNCOMPRESSED:
                raise PriceFeedError('Размер распакованной таблицы больше 20 МБ')
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        raise PriceFeedError('Не удалось открыть Excel-файл') from exc
    try:
        if 'Прайс' not in workbook.sheetnames:
            raise PriceFeedError('Не найден лист «Прайс»')
        rows = workbook['Прайс'].iter_rows(values_only=True)
        headers = None
        products = {}
        for row_index, row in enumerate(rows, 1):
            if row_index > 5000:
                raise PriceFeedError('В таблице больше 5000 строк')
            values = list(row)
            if headers is None:
                cells = {str(v).strip().casefold(): i for i, v in enumerate(values) if v is not None}
                if {'наименование', 'кол-во', 'цена от 1 шт'} <= cells.keys():
                    headers = cells
                continue
            def cell(key):
                i = headers[key]
                return values[i] if i < len(values) else None
            title, stock, price = cell('наименование'), cell('кол-во'), cell('цена от 1 шт')
            if title is None and stock is None and price is None:
                continue
            if isinstance(title, str) and title.strip().casefold().startswith('файл выгружен для:') and stock is None and price is None:
                continue
            if not isinstance(title, str) or not 2 <= len(title.strip()) <= 240:
                raise PriceFeedError(f'Строка {row_index}: неверное название')
            stock_text = str(stock).strip() if stock is not None else ''
            if not re.fullmatch(r'\d+\+?', stock_text):
                raise PriceFeedError(f'Строка {row_index}: не удалось прочитать остаток')
            # A plus means at least this many, not a precise upper bound.
            # Conservatively offer at most that lower bound.
            quantity = int(stock_text.rstrip('+'))
            if quantity > 1_000_000:
                raise PriceFeedError(f'Строка {row_index}: неверный остаток')
            try:
                if isinstance(price, bool) or not isinstance(price, (str, int, float, Decimal)):
                    raise CatalogError('нет цены')
                amount = parse_price(str(price))
                if amount is None:
                    raise CatalogError('нет цены')
            except CatalogError as exc:
                raise PriceFeedError(f'Строка {row_index}: неверная цена от 1 шт') from exc
            regions = FLAGS.findall(title)
            name = FLAGS.sub('', title).strip()
            warnings = ('multiple_regions',) if len(set(regions)) > 1 else ()
            product = Product(name=name, price=amount, stock=quantity, country=regions[0] if regions else '', warnings=warnings)
            key = product_key(product)
            if key in products:
                raise PriceFeedError(f'Строка {row_index}: повтор позиции «{name[:80]}»')
            products[key] = product
        if headers is None or not products:
            raise PriceFeedError('Не найдены товары и нужные колонки прайса Supernova')
        # The sheet provides no independent total: the number of parsed rows
        # alone cannot establish that an export is complete.
        return PriceSnapshot(tuple(products.values()), len(products), None, 0)
    finally:
        workbook.close()
