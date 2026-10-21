"""Local selling prices over an unchanged supplier catalog.

Category rules replace the common rule. This module never writes back to the
catalog and never derives a price when the supplier omitted it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_HALF_UP, localcontext

from .catalog import Product, normalize, parse_spec


CATEGORIES = {
    'phones': 'Телефоны',
    'tablets': 'Планшеты',
    'laptops': 'Ноутбуки',
    'wearables': 'Часы, трекеры и кольца',
    'headphones': 'Наушники',
    'glasses': 'Очки',
    'cameras': 'Камеры',
    'consoles': 'Игровые консоли',
    'accessories': 'Аксессуары и питание',
    'audio': 'Микрофоны и аудиоустройства',
    'other': 'Прочее',
}

_CATEGORY_ALIASES = {
    'default': ('общее', 'общая', 'общий', 'по умолчанию', 'все', 'all'),
    'phones': ('phone', 'телефон', 'телефоны', 'смартфон', 'смартфоны'),
    'tablets': ('tablet', 'планшет', 'планшеты'),
    'laptops': ('laptop', 'notebooks', 'ноутбук', 'ноутбуки'),
    'wearables': ('watch', 'watches', 'часы', 'трекеры', 'кольца', 'носимые'),
    'headphones': ('headphone', 'earbuds', 'наушники', 'гарнитуры'),
    'glasses': ('очки', 'smart glasses'),
    'cameras': ('camera', 'камеры', 'камера', 'фотоаппараты', 'фотоаппарат'),
    'consoles': ('console', 'консоль', 'консоли', 'приставки', 'игровые консоли'),
    'accessories': ('accessory', 'аксессуары', 'аксессуар', 'питание', 'зарядки'),
    'audio': ('аудио', 'микрофоны', 'микрофон', 'колонки'),
    'other': ('прочее', 'остальное', 'другое'),
}

_MODE_ALIASES = {
    'fixed': ('fixed', 'фикс', 'фиксированная', 'сумма', 'руб', 'рубли', 'рублей', '₽'),
    'percent': ('percent', 'percentage', 'процент', 'проценты', 'процентов', '%'),
}


def _label(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('Категория и режим должны быть текстом')
    value = unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')
    return re.sub(r'\s+', ' ', value.replace('_', ' ').replace('-', ' ')).strip()


def resolve_category(value: str) -> str:
    key = _label(value)
    for canonical, aliases in _CATEGORY_ALIASES.items():
        if key in {_label(canonical), *(_label(alias) for alias in aliases),
                   _label(CATEGORIES.get(canonical, 'Общее правило'))}:
            return canonical
    raise ValueError('Неизвестная категория. Допустимы default и: ' + ', '.join(CATEGORIES))


def _resolve_mode(value: str) -> str:
    key = _label(value)
    for canonical, aliases in _MODE_ALIASES.items():
        if key in aliases:
            return canonical
    raise ValueError('Режим наценки: fixed (рубли) или percent (проценты)')


@dataclass(frozen=True)
class MarkupRule:
    mode: str
    value: Decimal

    def __post_init__(self):
        mode = _resolve_mode(self.mode)
        object.__setattr__(self, 'mode', mode)
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise ValueError('Наценка должна быть конечным Decimal, без float')
        maximum = Decimal('100000000') if mode == 'fixed' else Decimal('1000')
        if not Decimal('0') <= self.value <= maximum:
            unit = 'рублей' if mode == 'fixed' else '%'
            raise ValueError(f'Наценка: от 0 до {maximum} {unit}')

    @classmethod
    def parse(cls, mode: str, value: str) -> MarkupRule:
        if not isinstance(value, str):
            raise ValueError('Значение наценки передай строкой, например 1500 или 7,5')
        raw = unicodedata.normalize('NFKC', value).strip()
        if not re.fullmatch(r'(?:\d+|\d{1,3}(?: \d{3})+)(?:[.,]\d+)?', raw):
            raise ValueError('Наценка — неотрицательное число с точкой или запятой, без exponent')
        return cls(mode, Decimal(raw.replace(' ', '').replace(',', '.')))


def parse_rule(mode: str, value: str) -> MarkupRule:
    return MarkupRule.parse(mode, value)


ZERO_MARKUP = MarkupRule('fixed', Decimal('0'))


def _rule_data(raw) -> MarkupRule:
    if not isinstance(raw, dict) or set(raw) != {'mode', 'value'}:
        raise ValueError('Правило наценки должно содержать только mode и value')
    return MarkupRule.parse(raw['mode'], raw['value'])


@dataclass(frozen=True)
class MarkupPolicy:
    default: MarkupRule = ZERO_MARKUP
    overrides: dict[str, MarkupRule] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.default, MarkupRule) or not isinstance(self.overrides, dict):
            raise ValueError('Неверная политика наценки')
        clean = {}
        for category, rule in self.overrides.items():
            key = resolve_category(category)
            if key == 'default' or key in clean or not isinstance(rule, MarkupRule):
                raise ValueError('Неверное или повторяющееся правило категории')
            clean[key] = rule
        object.__setattr__(self, 'overrides', clean)

    @classmethod
    def from_data(cls, raw=None) -> MarkupPolicy:
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or not set(raw).issubset({'default', 'overrides'}):
            raise ValueError('Неверный формат политики наценки')
        default = _rule_data(raw['default']) if 'default' in raw else ZERO_MARKUP
        overrides = raw.get('overrides', {})
        if not isinstance(overrides, dict):
            raise ValueError('Правила категорий должны быть объектом')
        return cls(default, {key: _rule_data(rule) for key, rule in overrides.items()})

    def to_data(self) -> dict:
        def rule_data(rule):
            return {'mode': rule.mode, 'value': format(rule.value, 'f')}
        return {'default': rule_data(self.default),
                'overrides': {key: rule_data(rule) for key, rule in sorted(self.overrides.items())}}

    def with_rule(self, category: str, rule: MarkupRule) -> MarkupPolicy:
        key = resolve_category(category)
        if not isinstance(rule, MarkupRule):
            raise ValueError('Неверное правило наценки')
        if key == 'default':
            return MarkupPolicy(rule, self.overrides)
        return MarkupPolicy(self.default, self.overrides | {key: rule})

    def without_rule(self, category: str) -> MarkupPolicy:
        key = resolve_category(category)
        if key == 'default':
            return MarkupPolicy(ZERO_MARKUP, self.overrides)
        return MarkupPolicy(self.default, {name: rule for name, rule in self.overrides.items() if name != key})


def category_for(product: Product) -> str:
    """Use explicit type/known family before a brand's numbered phone line."""
    n = normalize(product.name)
    if parse_spec(product.name).category != 'device':
        return 'accessories'
    if re.search(r'\b(?:adapter|адаптер\w*|кабель\w*|cable|charger|заряд\w*|'
                 r'power\s*bank|повербанк\w*|пауэрбанк\w*|solar\s+panel|солнечн\w*\s+панел\w*|'
                 r'controller|joy[ -]?con|dualsense|геймпад\w*|ремеш\w*|replacement\s+band|'
                 r'кассет\w*|картридж\w*|film|smarttag\d*|airtag\d*)\b', n) or re.search(r'\bdji\s+power\b', n):
        return 'accessories'
    if re.search(r'\b(?:mic|microphone|микрофон\w*|speaker|колонк\w*)\b', n):
        return 'audio'
    if (re.search(r'\binsta360\s+wave\b|\bmarshall\s+(?:emberton|stanmore|acton|woburn)\b', n)
            or re.search(r'\byandex\s+.*\b(?:станци\w*|station|стрит|street)\b', n)):
        return 'audio'
    if re.search(r'\b(?:airpods|buds|headphones|earbuds|наушник\w*|гарнитур\w*)\b', n):
        return 'headphones'
    if re.search(r'\bsony\s+(?:wh|wf)[ -]?\d|\bsennheiser\s+hd[ -]?\d', n):
        return 'headphones'
    if re.search(r'\b(?:watch|smartwatch|часы|браслет\w*|кольцо|ring)\b', n):
        return 'wearables'
    if (re.search(r'\bgarmin\s+(?:fenix|forerunner|vivoactive|venu|instinct|epix|enduro)\b', n)
            or re.search(r'\bcoros\s+(?:pace|apex|vertix)\b', n)
            or re.search(r'\bfitbit\s+(?:air|sense|charge|versa|inspire|luxe)\b', n)):
        return 'wearables'
    if re.search(r'\b(?:очки|sunglasses|optics)\b', n):
        return 'glasses'
    if (re.search(r'\brayban\s+.*\b(?:wayfarer|skyler|headliner|scriber|blayzer)\b', n)
            or re.search(r'\brayban\s+(?:wayfarer|skyler|headliner|scriber|blayzer)\b', n)
            or re.search(r'\boakley\s+(?:hstn|vanguard)\b', n)):
        return 'glasses'
    if re.search(r'\b(?:macbook|matebook|thinkpad|ноутбук\w*|laptop|notebook)\b|\b(?:galaxy|samsung)\s+book\b', n):
        return 'laptops'
    if (re.search(r'\b(?:ipad|tablet|планшет\w*)\b|\b(?:galaxy|samsung)\s+tab\b', n)
            or re.search(r'\b(?:xiaomi|redmi|poco|oneplus|honor)\s+pad\b', n)):
        return 'tablets'
    if (re.search(r'\bnintendo\s+switch\b|\bplaystation\s*\d|\bps[3456]\b', n)
            or re.search(r'\bxbox\s+(?:series|one|\d)', n)):
        return 'consoles'
    if re.search(r'\b(?:camera|камера|фотоаппарат\w*)\b', n):
        return 'cameras'
    if (re.search(r'\b(?:fujifilm\s+)?instax\s+(?:mini|evo|wide|square)\b', n)
            or re.search(r'\bgopro\s+(?:hero|max|mission)\b', n)
            or re.search(r'\binsta360\s+(?:ace\s*pro|acepro|go|x\d+|luna|link)\b', n)
            or re.search(r'\bdji\s+osmo\s*(?:pocket|action|\d)', n)):
        return 'cameras'
    if re.search(r'\b(?:iphone|smartphone|смартфон\w*|телефон\w*)\b', n):
        return 'phones'
    if (re.search(r'\b(?:galaxy|samsung)\s+(?:[sa]\d{2}|(?:z\s+)?(?:flip|fold)\s*\d|note\s*\d)', n)
            or re.search(r'\bpixel\s+\d', n)
            or re.search(r'\boneplus\s+(?:\d|nord\s*\d)', n)
            or re.search(r'\bhonor\s+(?:\d|magic\s*\d)', n)
            or re.search(r'\b(?:xiaomi|redmi)\s+(?:\d|note\s*\d|mix\s+flip)', n)
            or re.search(r'\bpoco\s+[fxmc]\d', n)
            or re.search(r'\brealme\s+(?:\d|[cg]\d|gt\s*\d)', n)
            or re.search(r'\bvivo\s+(?:[xsv]\s*\d)', n)
            or re.search(r'\bnothing\s+phone\s*\d', n)
            or re.search(r'^note\s*\d', n)):
        return 'phones'
    return 'other'


def price_products(products: list[Product], policy: MarkupPolicy) -> list[Product]:
    if not isinstance(policy, MarkupPolicy):
        raise ValueError('Неверная политика наценки')
    result = []
    for product in products:
        price = product.price
        if price is None:
            result.append(replace(product, price=None))
            continue
        if not isinstance(price, Decimal) or not price.is_finite() or price < 0:
            raise ValueError('Цена поставщика должна быть неотрицательным конечным Decimal')
        rule = policy.overrides.get(category_for(product), policy.default)
        # Enough decimal precision for the complete exact sum/product before
        # the one and only monetary rounding operation.
        precision = max(28, len(price.as_tuple().digits) + len(rule.value.as_tuple().digits)
                        + abs(price.as_tuple().exponent) + abs(rule.value.as_tuple().exponent) + 16)
        with localcontext() as context:
            context.prec = precision
            addition = rule.value if rule.mode == 'fixed' else price * rule.value / Decimal('100')
            selling_price = (price + addition).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        result.append(replace(product, price=selling_price))
    return result


def _amount(value: Decimal) -> str:
    integer, _, fraction = format(value, 'f').partition('.')
    grouped = f'{int(integer):,}'.replace(',', ' ')
    fraction = fraction.rstrip('0')
    return grouped + (',' + fraction if fraction else '')


def format_policy(policy: MarkupPolicy) -> str:
    def describe(rule: MarkupRule):
        if rule.value == 0:
            return 'без наценки'
        return '+' + _amount(rule.value) + (' ₽' if rule.mode == 'fixed' else '%')
    lines = ['Общее правило: ' + describe(policy.default)]
    if policy.overrides:
        lines += [CATEGORIES[key] + ': ' + describe(rule) + ' (вместо общего правила)'
                  for key, rule in sorted(policy.overrides.items())]
    else:
        lines.append('Отдельные правила категорий не заданы.')
    return '\n'.join(lines)
