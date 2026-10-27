"""Controlled spelling normalization; product identity is checked by the matcher.

The JSON contains readable allowlists. Unknown words and model digits survive;
this module never searches for a similar product, price, model or SKU.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path


DATA_PATH = Path(__file__).with_name('dictionaries') / 'recognition_aliases.json'
_GROUPS = ('brands', 'versions', 'features', 'series', 'conditions', 'colors')


def _base(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Не удалось прочитать словарь распознавания {path.name}') from exc
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('Неподдерживаемая версия словаря распознавания')
    for group in _GROUPS + ('contextual_versions',):
        entries = data.get(group)
        if not isinstance(entries, dict) or not entries:
            raise ValueError(f'Словарь {group} должен быть непустым объектом')
        for target, aliases in entries.items():
            if not isinstance(target, str) or not target.strip() or not isinstance(aliases, list) or not aliases:
                raise ValueError(f'Неверная запись словаря {group}')
            if any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
                raise ValueError(f'Пустой или неверный вариант в словаре {group}')
    units = data.get('units', {})
    if not isinstance(units, dict) or any(
            target not in {'gb', 'tb', 'mm'} or not isinstance(aliases, list) or not aliases
            or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
            for target, aliases in units.items()):
        raise ValueError('Неверная запись словаря units')
    owners = data.get('brand_owners')
    if not isinstance(owners, dict) or set(owners) != set(data['brands']):
        raise ValueError('Для каждой марки требуется запись brand_owners')
    if any(not isinstance(owner, str) or owner not in owners for owner in owners.values()):
        raise ValueError('brand_owners ссылается на неизвестную марку')
    fuzzy = data.get('fuzzy_brands')
    if not isinstance(fuzzy, list) or any(not isinstance(brand, str) or brand not in owners for brand in fuzzy):
        raise ValueError('fuzzy_brands содержит неизвестную марку')
    return data


DATA = _load(DATA_PATH)
BRAND_OWNERS = dict(DATA['brand_owners'])
COLORS = {color: tuple(dict.fromkeys(_base(a) for a in [color, *aliases]))
          for color, aliases in DATA['colors'].items()}
ALIASES: dict[str, str] = {}
for _group in _GROUPS:
    for _target, _aliases in DATA[_group].items():
        for _alias in [_target, *_aliases]:
            _key, _canonical = _base(_alias), _base(_target)
            if _key in ALIASES and ALIASES[_key] != _canonical:
                raise ValueError(f'Вариант {_key!r} относится к разным значениям словаря')
            ALIASES[_key] = _canonical


def _pattern(values) -> str:
    return '|'.join(re.escape(value) for value in sorted(set(values), key=lambda s: (-len(s), s)))


_ALIAS_PATTERN = re.compile(r'(?<!\w)(?:' + _pattern(ALIASES) + r')(?!\w)')
_BRAND_ALIASES = {_base(alias): _base(target) for target, aliases in DATA['brands'].items()
                  for alias in [target, *aliases]}
_PREFIX_ALIASES = dict(_BRAND_ALIASES)
for _target, _aliases in DATA['series'].items():
    for _alias in [_target, *_aliases]:
        _PREFIX_ALIASES[_base(_alias)] = _base(_target)
for _target, _aliases in DATA['versions'].items():
    for _alias in [_target, *_aliases]:
        _PREFIX_ALIASES[_base(_alias)] = _base(_target)
_PREFIX_PATTERN = re.compile(r'(?<!\w)(?:' + _pattern(_PREFIX_ALIASES) + r')(?=\d)')
_BRAND_ALPHA_PATTERN = re.compile(r'(?<!\w)(?:' + _pattern(_BRAND_ALIASES) + r')(?=[a-zа-я]{1,12}\d)')
_VERSION_ALIASES = {_base(alias): _base(target) for target, aliases in DATA['versions'].items()
                    for alias in [target, *aliases]}
_NUMBER_VERSION = re.compile(r'(?<!\w)(\d{1,3})(' + _pattern(_VERSION_ALIASES) + r')(?!\w)')
_SAMSUNG_VERSION = re.compile(r'\b([sa]\d{2})(' + _pattern(_VERSION_ALIASES) + r')(?!\w)')
_CONTEXTUAL = {_base(alias): _base(target) for target, aliases in DATA['contextual_versions'].items()
               for alias in aliases}
_SHORT_VERSION = re.compile(r'(?<!\w)(\d{1,2})\s*(' + _pattern(_CONTEXTUAL) + r')(?!\w)')
_FUZZY = {_base(alias): _base(target) for target in DATA['fuzzy_brands']
          for alias in [target, *DATA['brands'][target]]
          if _base(alias).isalpha() and len(_base(alias)) >= 6}
_UNITS = {_base(alias): target for target, aliases in DATA.get('units', {
    'gb': ['gb', 'гб'], 'tb': ['tb', 'тб'], 'mm': ['mm', 'мм']}).items() for alias in aliases}
_UNIT_PATTERN = re.compile(r'\b(\d+)\s*(' + _pattern(_UNITS) + r')\b')


def _one_edit(left: str, right: str) -> bool:
    """One insertion, deletion, substitution or adjacent transposition."""
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        indexes = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        return len(indexes) == 1 or (len(indexes) == 2 and indexes[1] == indexes[0] + 1
                                     and left[indexes[0]] == right[indexes[1]]
                                     and left[indexes[1]] == right[indexes[0]])
    short, long = (left, right) if len(left) < len(right) else (right, left)
    i = next((i for i, (a, b) in enumerate(zip(short, long)) if a != b), len(short))
    return short[i:] == long[i + 1:]


@lru_cache(maxsize=4096)
def _fuzzy_brand(token: str) -> str | None:
    if len(token) < 5 or not token.isalpha() or token in ALIASES:
        return None
    candidates = {target for alias, target in _FUZZY.items() if _one_edit(token, alias)}
    return next(iter(candidates)) if len(candidates) == 1 else None


@lru_cache(maxsize=4096)
def normalize_with_notes(value: str) -> tuple[str, tuple[str, ...]]:
    text = _base(value)
    notes: list[str] = []
    # Sony WH/WF prefixes are model codes, even if written with a space.
    text = re.sub(r'\b(wh|wf)\s+(1000xm\d+)\b', r'\1-\2', text)
    protected: dict[str, str] = {}
    marker = 'zzlexprotected'
    while marker in text:
        marker += 'z'

    def protect_code(match):
        token = match[0]
        if not re.search(r'\d', token):
            return token
        # Natural joined product names remain normalizable. Technical codes
        # such as IP17-REFURB must not acquire meaning from their suffixes.
        if any(re.match(re.escape(alias) + r'\d', token) for alias in _BRAND_ALIASES):
            return token
        key = f'{marker}{len(protected)}zz'
        protected[key] = token
        return key

    text = re.sub(r'(?<!\w)[a-zа-я][a-zа-я0-9]*(?:[-/][a-zа-я0-9]+)+(?!\w)', protect_code, text)

    def record(source: str, target: str) -> str:
        if source != target:
            note = f'{source} → {target}'
            if note not in notes:
                notes.append(note)
        return target

    # Boundaries are opened only at an allowlisted word followed by a model number.
    def split_prefix(match):
        tail = re.match(r'\d+\w*', match.string[match.end():])[0]
        record(match[0] + tail, _PREFIX_ALIASES[match[0]] + ' ' + tail)
        return _PREFIX_ALIASES[match[0]] + ' '

    # Prefer a complete known brand before digits over a shorter alias prefix
    # (e.g. «айфон17» must not be split as the abbreviation «айф» + «он17»).
    text = _PREFIX_PATTERN.sub(split_prefix, text)
    for _ in range(2):
        def split_brand_code(match):
            tail = re.match(r'[a-zа-я]+\d+\w*', match.string[match.end():])[0]
            record(match[0] + tail, _BRAND_ALIASES[match[0]] + ' ' + tail)
            return _BRAND_ALIASES[match[0]] + ' '
        text = _BRAND_ALPHA_PATTERN.sub(split_brand_code, text)
    text = _PREFIX_PATTERN.sub(split_prefix, text)
    text = re.sub(r'(?<!\w)(\d{1,2})\s*(?:sim|сим)(?!\w)',
                  lambda m: record(m[0], m[1] + ' sim'), text)
    text = _SHORT_VERSION.sub(lambda m: record(m[0], m[1] + ' ' + _CONTEXTUAL[m[2]]), text)
    text = _NUMBER_VERSION.sub(lambda m: record(m[0], m[1] + ' ' + _VERSION_ALIASES[m[2]]), text)
    text = _ALIAS_PATTERN.sub(lambda m: record(m[0], ALIASES[m[0]]), text)

    # Only a whole alphabetic brand word near numeric device information may be
    # corrected. SKU/model tokens containing digits or punctuation are untouched.
    lines = []
    for line in text.split('\n'):
        if re.search(r'\d', line):
            def correct(match):
                token = match[0]
                target = _fuzzy_brand(token)
                return record(token, target) if target else token
            line = re.sub(r'(?<![\w-])[a-zа-я]{5,}(?![\w-])', correct, line)
        lines.append(line)
    text = '\n'.join(lines)
    if re.search(r'\b(?:samsung|galaxy)\b', text):
        text = re.sub(r'\b([са])(?=\d{2}[a-z]?\b)',
                      lambda m: record(m[0], {'с': 's', 'а': 'a'}[m[1]]), text)
        text = re.sub(r'\b([са])(?=\d{2}(?:ultra|ультра|plus|плюс)\b)',
                      lambda m: record(m[0], {'с': 's', 'а': 'a'}[m[1]]), text)
    text = _SAMSUNG_VERSION.sub(lambda m: record(m[0], m[1] + ' ' + _VERSION_ALIASES[m[2]]), text)
    text = re.sub(r'\b([sa]\d{2})\s*\+', lambda m: record(m[0], m[1] + ' plus'), text)
    text = _UNIT_PATTERN.sub(lambda m: record(m[0], m[1] + _UNITS[m[2]]), text)
    # Keep line structure for multi-product requests; punctuation carries meaning.
    text = '\n'.join(re.sub(r'[\t\r\f\v ]+', ' ', line).strip() for line in text.split('\n')).strip()
    for key, token in protected.items():
        text = text.replace(key, token)
    return text, tuple(notes)


def normalize_text(value: str) -> str:
    return normalize_with_notes(value)[0]


def lexicon_stats() -> dict[str, int]:
    return {'brand_groups': len(DATA['brands']), 'color_groups': len(COLORS),
            'alias_spellings': len(ALIASES), 'fuzzy_brand_targets': len(DATA['fuzzy_brands'])}
