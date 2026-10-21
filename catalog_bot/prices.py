from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import CatalogError, Product, normalize, parse_price, product_key

FLAGS = re.compile(r'[\U0001F1E6-\U0001F1FF]{2}')
PRICE_LINE = re.compile(r'^.*?\bот\s+(\d+)\s*шт(?:\.|ук[аи]?)?\s*[-–—:]\s*(\d[\d\s.,]*?)(?:\s*(?:₽|руб\.?))?\s*$', re.I)
FOOTER = re.compile(r'всего\s+найдено\s*:?\s*(\d+)\s+товар', re.I)


class PriceFeedError(CatalogError):
    pass


class IncompletePrice(PriceFeedError):
    pass


@dataclass(frozen=True)
class PriceSnapshot:
    products: tuple[Product, ...]
    raw_count: int
    declared_count: int | None
    duplicates: int

    @property
    def complete(self):
        return self.declared_count is not None and self.raw_count == self.declared_count


def clean_line(text: str) -> str:
    return text.replace('**', '').replace('__', '').replace('\u00a0', ' ').replace('\u202f', ' ').strip()


def parse_price_feed(text: str, *, require_complete: bool = False) -> PriceSnapshot:
    if len(text.encode('utf-8')) > 1_000_000:
        raise PriceFeedError('Прайс больше 1 МБ')
    pending: str | None = None
    last_name: str | None = None
    declared: int | None = None
    raw_count = 0
    duplicates = 0
    products: dict[str, Product] = {}
    last_had_price = False
    for index, raw in enumerate(text.splitlines(), 1):
        line = clean_line(raw)
        if not line or line.startswith('#'):
            continue
        footer = FOOTER.search(line)
        if footer:
            if declared is not None and declared != int(footer[1]):
                raise PriceFeedError('Несколько разных итоговых количеств в одном прайсе')
            declared = int(footer[1])
            continue
        if re.match(r'^цены\s+на\b|^прайс(?:\s|$)', normalize(line)):
            continue
        price = PRICE_LINE.match(line)
        if price:
            title = pending or last_name
            if not title:
                raise PriceFeedError(f'Строка {index}: цена без названия товара')
            minimum = int(price[1])
            if not 1 <= minimum <= 999999:
                raise PriceFeedError(f'Строка {index}: неверное минимальное количество')
            title_regions = FLAGS.findall(title)
            price_regions = FLAGS.findall(line)
            country = title_regions[0] if title_regions else ''
            price_country = price_regions[0] if price_regions else ''
            name = re.sub(r'^\s*[•▪▫◦·*-]\s*', '', FLAGS.sub('', title)).strip()
            if not 2 <= len(name) <= 240:
                raise PriceFeedError(f'Строка {index}: неверное название товара')
            warnings = []
            if country and price_country and country != price_country:
                warnings.append('region_conflict')
            if len(set(title_regions)) > 1 or len(set(price_regions)) > 1:
                warnings.append('multiple_regions')
            # "От 1 шт" is a price threshold, never an inventory quantity.
            product = Product(name, parse_price(price[2]), stock=None, country=country,
                              price_country=price_country, min_qty=minimum, warnings=tuple(warnings))
            key = product_key(product)
            if key in products:
                if products[key].price != product.price:
                    raise PriceFeedError(f'Разные цены на одну и ту же позицию: {name}')
                duplicates += 1
            else:
                products[key] = product
            if pending:
                raw_count += 1
            last_name, pending, last_had_price = title, None, True
            continue
        if re.search(r'\bот\s+\d+\s+шт', line, re.I):
            raise PriceFeedError(f'Строка {index}: не удалось прочитать цену')
        if pending is not None:
            raise IncompletePrice(f'Не найдена цена для «{pending[:80]}»')
        pending, last_had_price = line, False
    if declared == 0 and not products and pending is None:
        return PriceSnapshot((), 0, 0, 0)
    if pending is not None or not last_had_price:
        raise IncompletePrice('Прайс обрывается до цены последней позиции')
    if not products:
        raise PriceFeedError('В сообщении нет товарных позиций с ценами')
    if len(products) > 3000:
        raise PriceFeedError('В прайсе больше 3000 предложений')
    if declared is not None and raw_count != declared:
        raise IncompletePrice(f'Получено {raw_count} товарных блоков из {declared}. Нужны остальные части прайса')
    if require_complete and declared is None:
        raise IncompletePrice('Нет итоговой строки «Всего найдено N товаров»: полнота прайса не подтверждена')
    return PriceSnapshot(tuple(products.values()), raw_count, declared, duplicates)
