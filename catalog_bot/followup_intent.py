"""Controlled follow-up intents and selection from an already quoted snapshot.

These helpers do not load inventory, send messages, change prices, or reserve
stock. The caller must verify the sender and the quote's lifetime first.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from .catalog import Product, attribute_reason, build_reply, find_products, parse_spec


_HOLD_WORD = re.compile(
    r'\b(?:отложи(?:те|ть|шь)?|заброниру(?:й(?:те)?|ешь|ете)|'
    r'забронировать|брониру(?:й(?:те)?|ешь|ете)|бронировать|'
    r'(?:за)?резервиру(?:й(?:те)?|ешь|ете)|(?:за)?резервировать|'
    r'бронь|брони|резерв)\b')
_DISCOUNT_VERB = re.compile(
    r'\b(?:скин(?:ь(?:те)?|ешь|ете|уть)|уступ(?:и(?:те)?|ишь|ите|ить)|'
    r'торг(?:оваться|уемся|уешься|уетесь)?|подешевле|дешевле|'
    r'подвин(?:ься|етесь|ешься)|сниз(?:ь(?:те)?|ишь|ите|ить))(?=\b|\d)')
_DISCOUNT_NOUN = re.compile(r'\bскидк[ауие]\b')
_MINUS_WORD = re.compile(r'\bминус\s*\d+(?:[.,]\d{1,2})?\b')
_SIGNED_AMOUNT = re.compile(r'(?<![\w/])[-−]\s*\d+(?:[.,]\d{1,2})?(?!\w)')
_NEGATIVE_PREFIX = re.compile(
    r'\b(?:не|нет|без)\s+(?:(?:надо|нужно|нужна|нужен|нужны|надобности|нужды|'
    r'прошу|просил|хочу|требуется|мне|нам|никакой|всякой|ставь|ставьте|'
    r'оформляй|оформляйте|делай|делайте|пожалуйста)\s+){0,4}$')
_NEGATIVE_SUFFIX = re.compile(
    r'^[\s,;:—–-]+(?:(?:мне|нам|пока|уже|больше)\s+){0,2}'
    r'(?:не\s+(?:надо|нужно|нужна|нужен|нужны|требуется|ставьте|делайте|оформляйте)|нет)\b')
_CANCEL_HOLD = re.compile(
    r'\b(?:отмен\w*|сним\w*|убер\w*|разброниру\w*|отбой)\b.*\b(?:брон\w*|резерв\w*)\b|'
    r'\b(?:брон\w*|резерв\w*)\b.*\b(?:отмен\w*|снят\w*)\b')
_HOLD_STATUS = re.compile(
    r'\b(?:у\s+(?:меня|вас|него|нее)|есть|чья|какая)\s+(?:уже\s+)?(?:бронь|брони|резерв)\b|'
    r'\b(?:бронь|резерв)\s+(?:есть|уже\s+есть)\b|\b(?:в|под)\s+(?:брони|бронью|резерве|резервом)\b')
_COUNTRY = re.compile(r'[\U0001F1E6-\U0001F1FF]{2}')


def _text(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е').strip()


def _negated(text: str, match: re.Match) -> bool:
    return bool(_NEGATIVE_PREFIX.search(text[:match.start()])
                or _NEGATIVE_SUFFIX.search(text[match.end():]))


def _hold_requested(text: str) -> bool:
    if _CANCEL_HOLD.search(text) or _HOLD_STATUS.search(text):
        return False
    return any(not _negated(text, match) for match in _HOLD_WORD.finditer(text))


def _discount_requested(text: str) -> bool:
    for expression in (_DISCOUNT_VERB, _MINUS_WORD):
        if any(not _negated(text, match) for match in expression.finditer(text)):
            return True
    for match in _DISCOUNT_NOUN.finditer(text):
        if _negated(text, match):
            continue
        # A statement that a price already includes a discount is not a new
        # request to reduce it. Keep explicit requests and questions readable.
        before, after = text[:match.start()], text[match.end():]
        if re.search(r'\b(?:со|с)\s+$', before) or re.match(r'\s+(?:уже\s+)?учтена\b', after):
            continue
        return True
    for match in _SIGNED_AMOUNT.finditer(text):
        before = text[:match.start()].rstrip()
        if re.search(r'(?:^|[^\w])\d+(?:[.,]\d+)?$', before):
            continue  # A numeric range such as 100 - 200 is not a discount.
        if not _negated(text, match):
            return True
    return False


def classify_followup(text: str) -> str | None:
    """Recognize only discount/hold requests; a discount always takes priority."""
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return None
    normalized = _text(text)
    if _discount_requested(normalized):
        return 'discount'
    if _hold_requested(normalized):
        return 'hold'
    return None


def strip_hold_request(text: str) -> str:
    """Remove request wording while keeping product identity and attributes."""
    normalized = _text(text)
    normalized = re.sub(r'\b(?:без\s+скидк[аи]|скидк[аи]\s+не\s+(?:нужна|надо|нужно)|'
                        r'торг\s+не\s+(?:нужен|надо|нужно)|не\s+(?:нужен|надо|нужно)\s+торг)\b',
                        ' ', normalized)
    normalized = _HOLD_WORD.sub(' ', normalized)
    normalized = re.sub(r'\b(?:поставь(?:те)?|поставить|сделай(?:те)?|оформи(?:те)?|'
                        r'можно|можешь|можете|пожалуйста|пжлст|пж|мне|нам|меня|'
                        r'этот|эту|это|его|ее|данный|данную|товар|позицию|вариант)\b', ' ', normalized)
    normalized = re.sub(r'\b(?:до|на)\s+(?:вечера|утра|завтра|сегодня|час|часок|день)\b', ' ', normalized)
    normalized = re.sub(r'\bна\s+\d+\s*(?:час(?:а|ов)?|минут(?:у|ы)?|дн(?:я|ей))\b', ' ', normalized)
    normalized = re.sub(r'\bдо\s+\d{1,2}:\d{2}\b', ' ', normalized)
    normalized = re.sub(r'\b(?:в|на|для|под|а|ну|давай|давайте)\b', ' ', normalized)
    return re.sub(r'\s+', ' ', normalized).strip(' ,.!?:;—–-')


def resolve_hold(text: str, products: Sequence[Product]) -> Product | None:
    """Resolve one item strictly within the quote; never guess between offers.

    With several items, a full model is matched by the catalog's strict matcher.
    An attribute-only clarification (e.g. ``черный 512``) can narrow the snapshot
    only when every item has the same model, version, brand and product line.
    Region or memory ambiguity still leaves more than one candidate and returns
    None. The caller can then ask the buyer to choose a specific position.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return None
    normalized = _text(text)
    if _discount_requested(normalized) or _CANCEL_HOLD.search(normalized):
        return None
    hold_words = list(_HOLD_WORD.finditer(normalized))
    if hold_words and not _hold_requested(normalized):
        return None
    quoted = [product for product in products if product.eligible]
    if not quoted:
        return None
    clean = strip_hold_request(text)
    if not clean:
        return quoted[0] if len(products) == len(quoted) == 1 and _hold_requested(normalized) else None
    # A copied offer includes its region, price and currency. Compare the
    # complete rendered row before parsing those numbers as model attributes.
    copied = [p for p in quoted if any(
        clean == strip_hold_request(build_reply([p], intro='', include_codes=legacy))
        for legacy in (False, True))]
    if copied:
        return copied[0] if len(copied) == 1 else None
    request = parse_spec(clean)
    if request.issues:
        return None
    if request.model:
        _, found = find_products('Куплю ' + clean, quoted)
        return found[0] if len(found) == 1 else None
    identities = {(item.family, item.model, item.variants, item.brand, item.product_line, item.category)
                  for item in (parse_spec(product.name) for product in quoted)}
    if len(identities) != 1:
        return None
    regions = set(_COUNTRY.findall(clean))
    explicit_attributes = (request.storage or request.ram is not None or request.colors or request.sim
                           or request.condition or request.size or request.traits or request.variants
                           or request.nfc is not None or request.connectivity or request.role_colors
                           or request.max_price is not None or regions)
    if not explicit_attributes:
        return None
    found = []
    for product in quoted:
        item = parse_spec(product.name)
        if request.variants and request.variants != item.variants:
            continue
        if attribute_reason(request, item):
            continue
        if regions and not regions.intersection({product.country, product.price_country}):
            continue
        if request.max_price is not None and (product.price is None or product.price > request.max_price):
            continue
        found.append(product)
    return found[0] if len(found) == 1 else None
