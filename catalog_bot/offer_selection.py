"""Choose regional offers without merging different product configurations."""
from __future__ import annotations

import unicodedata
import re

from .catalog import Product


_SAMSUNG_PHONE = re.compile(r'\bgalaxy\s+(?:s\d+|a\d+|z\s*(?:flip|fold))', re.IGNORECASE)
_SAMSUNG_PHONE_CODE = re.compile(r'(?<!\w)SM-[SAF]\d{3}[A-Z0-9]*(?:/[A-Z0-9]+)?(?!\w)', re.IGNORECASE)


def _offer_name(name: str) -> str:
    # Supplier model codes on Samsung phones distinguish regional offers. They
    # are hidden in the response, so they must not create duplicate visible rows.
    # Keep other codes: e.g. laptop model codes can distinguish different goods.
    if _SAMSUNG_PHONE.search(name):
        name = _SAMSUNG_PHONE_CODE.sub('', name)
    return ' '.join(unicodedata.normalize('NFKC', name).casefold().split())


def select_offers(products: list[Product]) -> list[Product]:
    """Keep one lowest-priced offer per product configuration.

    Keep explicit SIM details, condition and other specifications distinct.
    Prefer priced offers; if none has a price, retain one unpriced offer.
    """
    groups: dict[tuple[str, str, int], list[tuple[int, Product]]] = {}
    for index, product in enumerate(products):
        name = _offer_name(product.name)
        sku = unicodedata.normalize('NFKC', product.sku).casefold().strip()
        groups.setdefault((name, sku, product.min_qty), []).append((index, product))

    chosen = []
    for offers in groups.values():
        priced = [item for item in offers if item[1].price is not None]
        chosen.append(min(priced, key=lambda item: (item[1].price, item[0])) if priced else offers[0])
    return [product for _, product in sorted(chosen, key=lambda item: item[0])]
