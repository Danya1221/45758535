"""Offline recognition acceptance checks against an independent, synthetic oracle.

The corpus is test data only. It never changes the application's working catalogue.
"""
from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path

from .catalog import Product, find_products, parse_price

DEFAULT_CORPUS = Path(__file__).resolve().parent.parent / 'examples' / 'recognition_cases.json'
MAX_CASES = 50_000


@dataclass(frozen=True)
class CheckCounts:
    total: int
    passed: int


@dataclass(frozen=True)
class Failure:
    case_id: str
    query: str
    expected: tuple[str, ...]
    actual: tuple[str, ...]
    category: str
    source: str
    expected_classification: str | None = None
    actual_classification: str | None = None


@dataclass(frozen=True)
class CheckResult:
    total: int
    passed: int
    failures: tuple[Failure, ...]
    categories: dict[str, CheckCounts]
    sources: dict[str, CheckCounts]


@dataclass(frozen=True)
class CheckCase:
    case_id: str
    query: str
    expected: tuple[str, ...]
    category: str
    source: str
    allow_bare: bool = True
    classification: str | None = None
    catalog_skus: tuple[str, ...] | None = None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError('Корпус распознавания: ' + message)


def _string(value, label: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), label + ' — непустая строка')
    return value


def _case(row: dict, known_skus: set[str], source: str) -> CheckCase:
    _require(isinstance(row, dict), 'сценарий должен быть объектом')
    _require(not set(row) - {'id', 'query', 'expected', 'category', 'allow_bare', 'classification', 'catalog_skus', 'note'},
             'неизвестные поля сценария')
    case_id = _string(row.get('id'), 'id сценария')
    query = _string(row.get('query'), f'{case_id}: query')
    _require(len(query) <= 12_000, f'{case_id}: query длиннее 12000 символов')
    category = _string(row.get('category'), f'{case_id}: category')
    expected = row.get('expected')
    _require(isinstance(expected, list) and all(isinstance(sku, str) for sku in expected),
             f'{case_id}: expected — список SKU')
    _require(len(expected) == len(set(expected)), f'{case_id}: повтор SKU в expected')
    _require(set(expected).issubset(known_skus), f'{case_id}: неизвестный SKU в expected')
    allow_bare = row.get('allow_bare', True)
    _require(isinstance(allow_bare, bool), f'{case_id}: allow_bare — boolean')
    classification = row.get('classification')
    _require(classification in {None, 'buy', 'bare', 'sell', 'uncertain'},
             f'{case_id}: неверная classification')
    catalog_skus = row.get('catalog_skus')
    if catalog_skus is not None:
        _require(isinstance(catalog_skus, list) and all(isinstance(sku, str) for sku in catalog_skus),
                 f'{case_id}: catalog_skus — список SKU')
        _require(len(catalog_skus) == len(set(catalog_skus)) and set(catalog_skus).issubset(known_skus),
                 f'{case_id}: неизвестный/повторный SKU в catalog_skus')
        _require(set(expected).issubset(catalog_skus), f'{case_id}: expected за пределами catalog_skus')
    return CheckCase(case_id, query, tuple(sorted(expected)), category, source, allow_bare, classification,
                     tuple(catalog_skus) if catalog_skus is not None else None)


def load_corpus(path: Path | None = None) -> tuple[tuple[Product, ...], tuple[CheckCase, ...]]:
    """Validate schema and expand only explicitly enumerated, deterministic queries."""
    target = Path(path) if path is not None else DEFAULT_CORPUS
    _require(target.stat().st_size <= 10_000_000, 'JSON больше 10 МБ')
    try:
        data = json.loads(target.read_text(encoding='utf-8-sig'))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError('Корпус распознавания: неверный JSON/UTF-8') from exc
    _require(isinstance(data, dict) and data.get('schema_version') == 1, 'нужен schema_version: 1')
    _require(not set(data) - {'schema_version', 'metadata', 'products', 'cases', 'generated_groups'},
             'неизвестные поля верхнего уровня')
    raw_products = data.get('products')
    _require(isinstance(raw_products, list) and 0 < len(raw_products) <= 3000, 'products: от 1 до 3000 товаров')
    products = []
    known_skus = set()
    allowed_product_fields = {'sku', 'name', 'price', 'stock', 'country', 'price_country', 'min_qty', 'warnings'}
    for row in raw_products:
        _require(isinstance(row, dict) and not set(row) - allowed_product_fields, 'неверные поля товара')
        sku = _string(row.get('sku'), 'SKU товара')
        _require(sku not in known_skus, 'повтор SKU: ' + sku)
        name = _string(row.get('name'), sku + ': name')
        _require(len(sku) <= 80 and 2 <= len(name) <= 240, sku + ': слишком длинные SKU/name')
        raw_price = row.get('price')
        _require(raw_price is None or isinstance(raw_price, (str, int)) and not isinstance(raw_price, bool),
                 sku + ': price — строка/целое число/null')
        price = parse_price(str(raw_price)) if raw_price is not None else None
        stock = row.get('stock', 1)
        _require(stock is None or type(stock) is int and 0 <= stock <= 999999, sku + ': неверный stock')
        min_qty = row.get('min_qty', 1)
        _require(type(min_qty) is int and 1 <= min_qty <= 999999, sku + ': неверный min_qty')
        warnings = row.get('warnings', [])
        _require(isinstance(warnings, list) and all(isinstance(w, str) for w in warnings), sku + ': неверные warnings')
        country, price_country = row.get('country', ''), row.get('price_country', '')
        _require(isinstance(country, str) and isinstance(price_country, str), sku + ': неверный регион')
        products.append(Product(name, price, stock, sku, country, price_country, min_qty, tuple(warnings)))
        known_skus.add(sku)
    raw_cases = data.get('cases', [])
    raw_groups = data.get('generated_groups', [])
    _require(isinstance(raw_cases, list) and isinstance(raw_groups, list), 'cases/generated_groups — списки')
    cases = [_case(row, known_skus, 'handwritten') for row in raw_cases]
    for group in raw_groups:
        _require(isinstance(group, dict), 'группа должна быть объектом')
        _require(not set(group) - {'id', 'category', 'expected', 'parts', 'allow_bare', 'classification', 'catalog_skus', 'note'},
                 'неизвестные поля generated_group')
        group_id = _string(group.get('id'), 'id группы')
        parts = group.get('parts')
        _require(isinstance(parts, list) and 0 < len(parts) <= 30 and all(
            isinstance(part, list) and part and all(isinstance(text, str) for text in part) for part in parts),
            group_id + ': parts — непустые списки строк')
        _require(math.prod(map(len, parts)) + len(cases) <= MAX_CASES, 'слишком много производных сценариев')
        seen_queries = set()
        for combination in itertools.product(*parts):
            query = ''.join(combination)
            if query in seen_queries:
                continue
            seen_queries.add(query)
            row = {key: value for key, value in group.items() if key != 'parts'}
            row.update(id=f'{group_id}:{len(seen_queries):04d}', query=query)
            cases.append(_case(row, known_skus, 'generated'))
    _require(0 < len(cases) <= MAX_CASES, 'от 1 до 50000 сценариев')
    ids = [case.case_id for case in cases]
    _require(len(ids) == len(set(ids)), 'повтор id сценария')
    return tuple(products), tuple(cases)


def run_checks(path: Path | None = None) -> CheckResult:
    products, cases = load_corpus(path)
    failures = []
    categories: dict[str, list[int]] = {}
    sources: dict[str, list[int]] = {}
    passed = 0
    for case in cases:
        selected = list(products) if case.catalog_skus is None else [p for p in products if p.sku in case.catalog_skus]
        classification, found = find_products(case.query, selected, allow_bare=case.allow_bare)
        actual = tuple(sorted({product.sku for product in found}))
        success = actual == case.expected and (case.classification is None or classification == case.classification)
        passed += success
        for counts, key in ((categories, case.category), (sources, case.source)):
            counts.setdefault(key, [0, 0])
            counts[key][0] += 1
            counts[key][1] += success
        if not success:
            failures.append(Failure(case.case_id, case.query, case.expected, actual, case.category, case.source,
                                    case.classification, classification))
    return CheckResult(len(cases), passed, tuple(failures),
                       {key: CheckCounts(*value) for key, value in sorted(categories.items())},
                       {key: CheckCounts(*value) for key, value in sorted(sources.items())})
