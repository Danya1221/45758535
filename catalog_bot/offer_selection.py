"""Choose regional offers without merging different product configurations."""
from __future__ import annotations

import unicodedata

from .catalog import Product


def select_offers(products: list[Product]) -> list[Product]:
    """Keep the cheapest offer and, when available, the cheapest Kazakhstan offer.

    Group only identical full names and SKUs; manufacturer codes, SIM details,
    condition and all other characteristics remain part of the identity.
    Unknown prices are retained because they cannot safely be compared.
    """
    groups: dict[tuple[str, str, int], list[tuple[int, Product]]] = {}
    unknown: list[tuple[int, Product]] = []
    for index, product in enumerate(products):
        if product.price is None:
            unknown.append((index, product))
            continue
        name = ' '.join(unicodedata.normalize('NFKC', product.name).casefold().split())
        sku = unicodedata.normalize('NFKC', product.sku).casefold().strip()
        groups.setdefault((name, sku, product.min_qty), []).append((index, product))

    chosen = list(unknown)
    for offers in groups.values():
        cheapest = min(offers, key=lambda item: (item[1].price, item[0]))
        chosen.append(cheapest)
        kz = [item for item in offers if '🇰🇿' in (item[1].country, item[1].price_country)]
        if kz:
            kz_cheapest = min(kz, key=lambda item: (item[1].price, item[0]))
            if kz_cheapest[0] != cheapest[0]:
                chosen.append(kz_cheapest)
    return [product for _, product in sorted(chosen, key=lambda item: item[0])]
