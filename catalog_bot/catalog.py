from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from functools import lru_cache


from .lexicon import ALIASES, BRAND_OWNERS, COLORS, normalize_text, normalize_with_notes


VARIANTS = frozenset("pro max plus ultra mini lite air edge se xl fold fe".split())
FILLER = frozenset(
    "куплю купим ищу ищем нужен нужна нужно нужны интересует возьму предложите "
    "предлагайте пожалуйста у кого есть кто продаст wtb "
    "телефон смартфон колонка очки размер size цвет color память storage "
    "gb гб tb тб ram озу sim esim dual nano нано и или or gen поколение "
    "wi fi wifi cellular a brand new бюджет до за руб р rub в наличии".split()
)
BUY = re.compile(r"\b(?:интересует\s+цена|сколько\s+стоит|есть\s+ли(?:\s+в\s+наличии)?|"
                 r"хочу\s+купить|куплю|купим|ищу|ищем|нужен|нужна|нужно|нужны|интересует|"
                 r"возьму|предложите|предлагайте|wtb)\b|у\s+кого\s+есть|кто\s+продаст|\bпо\s*чем\b")
SELL = re.compile(r"\b(?:продам|продаю|продаем|продается|продажа|отдам|wts)\b")
CHATTER = re.compile(r"\b(?:купил|купила|обзор|сломался|сломал|спасибо|почему|настроить|обсуждаем)\b|у\s+меня")
MONEY = re.compile(r"\d\s*(?:₽|руб\b|rub\b|р\.)", re.I)


@lru_cache(maxsize=4096)
def normalize(value: str) -> str:
    return normalize_text(value)


def words(value: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", normalize(value))


def code_key(value: str) -> str:
    # Supplier identifiers are opaque: BK/BLACK, PM/ProMax and C/С stay
    # distinct. Only letter case, compatibility forms and separators normalize.
    value = unicodedata.normalize('NFKC', value).casefold()
    return re.sub(r"[^a-zа-яё0-9]", "", value)


IDENTIFIER_TOKEN = re.compile(r'[\w]+(?:[-/][\w]+)*')


def identifier_specification(token: str) -> str:
    """Keep explicit natural device identity when a SKU happens to collide.

    A code like IP17-USED is opaque. A canonical brand by itself or a natural
    form like iPhone17/Samsung-S26 cannot erase the requested manufacturer.
    Attribute words encoded in the rest of an ID are not user requirements.
    """
    normalized = normalize(token)
    if normalized in BRAND_OWNERS:
        return normalized
    brand_pattern = '|'.join(re.escape(brand) for brand in sorted(BRAND_OWNERS, key=len, reverse=True))
    natural = re.match(r'^(?:(' + brand_pattern + r'))[\s\-/]+([a-zа-яё]*\d+[a-zа-яё0-9]*)\b', normalized)
    if not natural:
        return ' '
    suffix = normalized[natural.end():].strip(' -/')
    versions = []
    for word in suffix.split():
        if word not in VARIANTS:
            break
        versions.append(word)
    return ' '.join((natural[1], natural[2], *versions))


def semantic_reference(token: str) -> bool:
    """A natural attribute/model word has priority over a coincident SKU."""
    normalized = normalize(token)
    tokens = set(words(normalized))
    semantic_words = (set(FILLER) | set(VARIANTS) | set(COLORS) | set(BRAND_OWNERS)
                      | {'nfc', 'no', 'new', 'used', 'refurbished', 'lte', 'pm', 'пм'})
    if tokens and tokens.issubset(semantic_words):
        return True
    if re.fullmatch(r'\d+(?:gb|tb)?', normalized):
        return True
    if re.fullmatch(r'(?:[sa]\d{2}[a-z]{0,2}|x\d{2,4}[a-z]?|gt\d{1,2}[a-z]?|\d{1,3}[a-z]{0,2})', normalized):
        return True
    if re.fullmatch(r'\d+\s*(?:sim|esim)', normalized):
        return True
    return False


@dataclass(frozen=True)
class Spec:
    family: str
    model: frozenset[str]
    variants: frozenset[str]
    storage: frozenset[int]
    ram: int | None
    colors: frozenset[str]
    sim: str | None
    condition: str | None
    size: str | None
    traits: frozenset[str]
    color_options: bool = False
    max_price: Decimal | None = None
    category: str = "device"
    nfc: bool | None = None
    connectivity: str | None = None
    issues: tuple[str, ...] = ()
    role_colors: frozenset[tuple[str, str]] = frozenset()
    brand: str | None = None
    product_line: str | None = None


# Family labels identify devices; in an accessory title they describe compatibility,
# not the manufacturer of the accessory. Explicit manufacturer words stay distinct.
COMPATIBILITY_FAMILIES = frozenset({'iphone', 'ipad', 'macbook', 'airpods', 'galaxy', 'pixel'})


def entity_text(text: str) -> str:
    return re.split(r'\b(?:для|for|compatible)\b', text, maxsplit=1)[0]


def identity(text: str, category: str) -> tuple[str | None, list[str]]:
    tokens = set(words(entity_text(text)))
    if category != 'device':
        tokens -= COMPATIBILITY_FAMILIES
    owners = {BRAND_OWNERS[token] for token in tokens if token in BRAND_OWNERS}
    # Meta makes both families; Ray-Ban and Oakley remain distinct identities.
    if 'meta' in owners and owners.intersection({'rayban', 'oakley'}):
        owners.remove('meta')
    # This complete combined label exists in the supplied catalog. The Fitbit
    # family stays explicit instead of becoming any Google-branded device.
    if {'google', 'fitbit'}.issubset(owners):
        owners.remove('google')
    if len(owners) > 1:
        return None, ['несовместимые бренды в одной строке: ' + ', '.join(sorted(owners))]
    return next(iter(owners), None), []


@lru_cache(maxsize=4096)
def parse_spec(text: str) -> Spec:
    n = normalize(text)
    n = re.sub(r"\b(?:до|бюджет)\s*[:=]?\s*[\d ]+(?:₽|руб|rub)?", " ", n)
    # Structured attributes are removed from model identity, never discarded.
    n = re.sub(r"\b(note|pixel|magic)(?=\d)", r"\1 ", n)
    tokens = set(words(n))
    category_text = n
    watch_body = ('watch' in words(entity_text(n))
                  and not re.search(r'\b(?:protective|cover|чехол\w*)\b', n)
                  and re.search(r'\b(?:aluminum|aluminium|steel|titanium|active\s+band)\b', n))
    if watch_body:
        category_text = re.sub(r'\bcase\b', '', n)
    category = next((kind for kind, pattern in (
        ('case', r'\b(?:чехол\w*|case|cover)\b'),
        ('protector', r'\b(?:стекло|пленка|protector)\b'),
        ('charger', r'\b(?:заряд\w*|charger|кабель|cable|adapter|адаптер\w*|power\s*bank|powerbank)\b'),
        ('accessory', r'\b(?:lens\s+kit|charging\s+pouch)\b'),
        ('parts', r'\b(?:дисплей|экран|аккумулятор|запчаст\w*|replacement)\b'),
    ) if re.search(pattern, category_text)), 'device')
    if (category == 'device' and re.search(r'\b(?:battery|batteries)\b', category_text)
            and not re.search(r'\b(?:bundle|combo)\b', category_text)):
        category = 'parts'
    brand, issues = identity(n, category)
    core = entity_text(n)
    core_tokens = set(words(core))
    product_line = None
    if category == 'device' and brand == 'xiaomi':
        subbrands = core_tokens.intersection({'redmi', 'poco'})
        if len(subbrands) > 1:
            issues.append('несовместимые линейки устройств в одной строке')
        product_line = next(iter(subbrands), 'xiaomi')
    nfc = False if re.search(r'\b(?:no|без)\s+nfc\b', n) else (True if 'nfc' in tokens else None)
    connectivity = 'cellular' if re.search(r'\b(?:cellular|lte)\b', n) else ('wifi' if re.search(r'\bwi[ -]?fi\b', n) else None)
    n = re.sub(r'\b(?:(?:no|без)\s+)?nfc\b|\b(?:cellular|lte|wi[ -]?fi)\b', ' ', n)
    colors = set()
    role_colors = set()
    color_aliases = sorted(((alias, color) for color, aliases in COLORS.items() for alias in aliases),
                           key=lambda pair: -len(pair[0]))
    for alias, color in color_aliases:
        pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
        if alias == 'wh':
            # WH is also the Sony headphone model prefix, not "white".
            pattern += r'(?!-\d)'
        if re.search(pattern, n):
            colors.add(color)
            for role, prefix in (('frame', r'f|frame|оправа'), ('lens', r'l|lens|линзы')):
                if re.search(r'\b(?:' + prefix + r')\s+(?:(?:shiny|matte|g15)\s+)?' + pattern, n):
                    role_colors.add((role, color))
            n = re.sub(pattern, " ", n)
    storage = set()
    memory_text = re.sub(r"\b\d{1,2}(?:gb)?\s*(?:ram|озу)\b", " ", n)
    memory_text = re.sub(r"\b\d{1,2}\s*/\s*(?=\d)", "", memory_text)
    for count, unit in re.findall(r"\b(\d{1,4})(gb|tb)\b", memory_text):
        storage.add(int(count) * (1024 if unit == "tb" else 1))
    pair = re.search(r"\b(\d{1,2})\s*/\s*(\d{1,4})(gb|tb)?\b", n)
    ram = int(pair[1]) if pair else None
    if pair:
        storage.add(int(pair[2]) * (1024 if pair[3] == 'tb' else 1))
    ram_match = re.search(r"\b(\d{1,2})(?:gb)?\s*(?:ram|озу)\b", n)
    if ram_match:
        ram = int(ram_match[1])
    if len(storage) > 1 and min(storage) <= 32:
        ram = ram or min(storage)
        storage.discard(min(storage))
    for capacity in (32, 64, 128, 256, 512, 1024, 2048):
        if re.search(rf"\b{capacity}\b", memory_text):
            storage.add(capacity)
    sim = None
    if re.search(r"\b(?:dual\s*sim|2\s*sim|две\s*sim)\b", n):
        sim = "dual_sim"
    elif re.search(r"\bsim\s*\+\s*esim\b|\besim\s*\+\s*sim\b", n):
        sim = "sim_esim"
    elif re.search(r"(?<!\w)sim(?!\w)", n):
        sim = "sim"
    elif re.search(r"(?<!\w)esim(?!\w)", n):
        sim = "esim"
    # Color names such as Ultra Violet must not create a model version.
    variant = set(words(n)) & VARIANTS
    family = "generic"
    model: set[str] = set()
    iphone = re.search(r"\biphone\s+(\d{1,2})\b", core) if category == 'device' else None
    short_iphone = re.search(r"^\s*(\d{1,2})\b", core) if category == 'device' else None
    if iphone:
        family, model = "iphone", {iphone[1]}
    elif short_iphone and (variant or storage) and 6 <= int(short_iphone[1]) <= 30:
        family, model = "iphone", {short_iphone[1]}
    elif "iphone" in core_tokens and category == 'device':
        family = "iphone"
    else:
        not_samsung_phone = category != 'device' or brand not in {None, 'samsung'} or re.search(r'\b(?:galaxy|samsung)\s+(?:tab|watch|buds|ring|book)\b', core) or bool(
            tokens & {'rayban', 'meta', 'oakley', 'sony', 'gopro', 'instax', 'fujifilm', 'garmin', 'coros', 'oura'})
        samsung = None if not_samsung_phone else re.search(r"\b([sa]\d{2}[a-z]?)\b", core)
        numbered = re.search(r"\b(ipad|pixel)\s+(\d{1,2})\b", core) if category == 'device' else None
        if samsung:
            family, model = "samsung", {samsung[1]}
        elif numbered:
            family, model = numbered[1], {numbered[2]}
    family_brand = {'iphone': 'apple', 'ipad': 'apple', 'pixel': 'google', 'samsung': 'samsung'}.get(family)
    if family_brand:
        if brand is not None and brand != family_brand:
            issues.append('бренд противоречит семейству модели')
        elif not any('бренд' in issue for issue in issues):
            brand = family_brand
    device_families = core_tokens.intersection(COMPATIBILITY_FAMILIES - {'galaxy'})
    if category == 'device' and len(device_families) > 1:
        issues.append('несколько семейств устройств в одной строке')
    cleaned = re.sub(r'(?<!\w)\d{3}-\d{5}-\d{2}(?!\w)', ' ', n)
    cleaned = re.sub(r"\b\d{1,4}(?:gb|tb)\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{1,2}\s*/\s*\d{1,4}\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:размер|size)\s+(?:[xsml]{1,3}|\d{1,2})\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:до|бюджет)\s*[:=]?\s*[\d ]+(?:₽|руб|rub)?", " ", cleaned)
    traits = {
        t for t in words(cleaned)
        if t not in FILLER and t not in VARIANTS and t not in {str(s) for s in storage}
        and (ram is None or t != str(ram))
        and t not in {"new", "used", "refurbished"}
        and not (t in BRAND_OWNERS and t == BRAND_OWNERS[t])
        and t != "galaxy"
    }
    if sim == "dual_sim":
        traits.discard("2")
    if re.search(r"\b1\s*sim\b", n):
        traits.discard("1")
    if family == "generic":
        model = traits
        if "rayban" in model:
            model -= {"m", "s", "l", "xl"}
        traits = set()
    else:
        traits -= model | {family}
    size_match = re.search(r"\b(?:размер|size)\s*([xsml]{1,3}|\d{1,2})\b", n)
    if not size_match and "rayban" in tokens:
        size_match = re.search(r"\b([sml]|xl|xxl)\s*$", n)
    condition = next((x for x in ("refurbished", "used", "new") if x in tokens), None)
    budget = re.search(r"\b(?:до|бюджет)\s*[:=]?\s*(\d[\d ]{2,9})(?:₽|руб|rub|$)", normalize(text))
    max_price = Decimal(budget[1].replace(" ", "")) if budget else None
    if re.search(r'\b(?:мят\w*|дефект\w*|витрин\w*|уцен\w*|поврежд\w*|damaged|open box|demo|renewed)\b', n):
        condition = 'damaged'
    if re.search(r"\s[-–—]\s*\d+\s*$", normalize(text)):
        issues.append("неясное число после дефиса; количество нужно указывать с «шт»")
    if len(tokens.intersection({"new", "used", "refurbished"})) > 1:
        issues.append("противоречивые требования к состоянию")
    if len(storage) > 1 and not re.search(r'\b\d+(?:gb|tb)?\s+(?:или|or)\s+\d+(?:gb|tb)?\b', n):
        issues.append('несколько значений памяти без «или»')
    # More than one explicit model on one line is unsafe to distribute attributes over.
    anchors = re.findall(r'\b(?:iphone|ipad|pixel|note)\s+\d+|\b[sa]\d{2}[a-z]?\b', n)
    if len(set(anchors)) > 1:
        issues.append('несколько моделей в одной строке; разделите их строками')
    ram_values = {int(a) for a in re.findall(r'\b(\d{1,2})\s*/\s*\d{1,4}', n)}
    ram_values.update(int(a) for a in re.findall(r'\b(\d{1,2})(?:gb)?\s*(?:ram|озу)\b', n))
    if len(ram_values) > 1:
        issues.append('несколько значений RAM')
    if 'sim' in tokens and 'esim' in tokens and sim != 'sim_esim':
        issues.append('неясная комбинация SIM: укажите SIM+eSIM или одну версию')
    if re.search(r'\b1\s*sim\b', n) and re.search(r'\b(?:2\s*sim|dual\s*sim|две\s*sim)\b', n):
        issues.append('противоречивые требования к числу SIM')
    return Spec(family, frozenset(model), frozenset(variant), frozenset(storage), ram,
                frozenset(colors), sim, condition, size_match[1] if size_match else None, frozenset(traits),
                bool(re.search(r'\b(?:' + '|'.join(re.escape(a) for values in COLORS.values() for a in values) + r')\s+(?:или|or)\s+(?:' + '|'.join(re.escape(a) for values in COLORS.values() for a in values) + r')\b', normalize(text))), max_price, category, nfc, connectivity, tuple(issues), frozenset(role_colors), brand, product_line)


def intent(text: str, allow_bare: bool) -> str:
    n = normalize(text)
    if SELL.search(n):
        return "sell"
    if re.search(r"\b(?:не|кроме|без)\b", re.sub(r"\bбез\s+nfc\b", "", n)):
        return "uncertain"
    # Attribute negations are deliberately not guessed.
    if re.search(r"\b(?:не|кроме|без)\s+(?:black|white|silver|esim|sim|черн\w*|бел\w*|серебр\w*)", n):
        return "uncertain"
    if BUY.search(n):
        return "buy"
    # A question about availability is a request; an ordinary stock notice is
    # still a sales message and must not start an automatic reply.
    if (re.search(r'\bв\s+наличии\s*\?\s*$', n) and not MONEY.search(n)
            and not re.search(r'\b(?:цена|стоимость)\s*[:=]?\s*\d', n)):
        return "buy"
    if re.search(r"\b(?:в наличии|прайс|оптом|распродажа)\b", n) or MONEY.search(n):
        return "sell"
    trailing_price = re.search(r"\b(\d{4,7})(?:[,.]\d{1,2})?\s*$", n)
    if trailing_price and int(trailing_price[1]) not in {1024, 2048}:
        return "sell"
    if allow_bare and not CHATTER.search(n):
        if len(words(n)) <= 24:
            return "bare"
        # A list of short product requests can exceed the single-title limit.
        # Keep every line subject to the same intent guards as an individual title.
        segments = request_segments(text) if '\n' in text else []
        if len(segments) > 1 and all(intent(segment, allow_bare=True) == 'bare' for segment in segments):
            return "bare"
    return "uncertain"


def request_segments(text: str) -> list[str]:
    """Keep intent headers; never combine two neighboring product lines."""
    result: list[str] = []
    header = ""
    for raw in text.splitlines():
        line = raw.strip(" \t•-–—")
        if not line:
            continue
        if BUY.fullmatch(normalize(line)):
            header = line
            continue
        if result and re.match(r"^(?:размер|size|цвет|color|память|ram|озу)\b", normalize(line)):
            result[-1] += " " + line
        else:
            result.append((header + " " + line).strip())
    return result[:20]


@dataclass(frozen=True)
class Product:
    name: str
    price: Decimal | None = None
    stock: int | None = 1
    sku: str = ""
    country: str = ""
    price_country: str = ""
    min_qty: int = 1
    warnings: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return (self.stock is None or self.stock > 0) and self.min_qty == 1 and not self.warnings


def product_key(product: Product) -> str:
    # Region and quantity threshold are part of the offer identity.
    return '\x1f'.join((normalize(product.name), product.country, product.price_country, str(product.min_qty)))


class CatalogError(ValueError):
    pass


def parse_price(value: str) -> Decimal | None:
    if not value.strip():
        return None
    value = re.sub(r"(?:₽|руб\.?|rub)", "", value.casefold()).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        raise CatalogError("цена должна быть числом в рублях")
    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise CatalogError("неверная цена") from exc
    if not 0 < price <= 100_000_000:
        raise CatalogError("цена должна быть больше нуля и не больше 100 млн")
    return price


def parse_catalog(text: str) -> list[Product]:
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise CatalogError("список пуст")
    if len(text.encode("utf-8")) > 1_000_000:
        raise CatalogError("список больше 1 МБ")
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise CatalogError("в списке нет товаров")
    delimiter = next((sep for sep in ("|", ";", "\t", ",") if sep in lines[0]), "|")
    try:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter, strict=True))
    except csv.Error as exc:
        raise CatalogError("неверный CSV: проверь кавычки и разделители") from exc
    headers = {"название": "name", "товар": "name", "name": "name", "цена": "price", "price": "price",
               "остаток": "stock", "количество": "stock", "stock": "stock", "артикул": "sku", "sku": "sku"}
    first = [normalize(cell.strip()) for cell in rows[0]]
    has_header = first[0] in {"название", "товар", "name"}
    columns = [headers.get(x, "") for x in first] if has_header else ["name", "price", "stock", "sku"]
    if has_header and ("" in columns or len(columns) != len(set(columns))):
        raise CatalogError("заголовки: название | цена | остаток | артикул")
    products: list[Product] = []
    seen_names: set[str] = set()
    seen_sku: set[str] = set()
    for index, row in enumerate(rows[1:] if has_header else rows, 2 if has_header else 1):
        try:
            if len(row) > len(columns):
                raise CatalogError("слишком много столбцов")
            cells = dict(zip(columns, (cell.strip() for cell in row)))
            name = cells.get("name", "").strip()
            if not has_header and len(row) == 1:
                inline_price = re.search(r"\s+[—–-]\s+(\d[\d .,]*(?:₽|руб\.?)?)\s*$", name)
                if inline_price:
                    cells["price"] = inline_price[1]
                    name = name[:inline_price.start()].strip()
            if not 2 <= len(name) <= 240 or not re.search(r"[a-zа-я]", normalize(name)):
                raise CatalogError("нужно название товара длиной 2–240 символов")
            if any(x in name for x in ("http://", "https://", "\x00")):
                raise CatalogError("в названии не должно быть ссылок")
            raw_stock = cells.get("stock", "") or "1"
            if not re.fullmatch(r"\d{1,6}", raw_stock):
                raise CatalogError("остаток — целое число от 0 до 999999")
            sku = cells.get("sku", "")
            if len(sku) > 80 or (sku and not code_key(sku)):
                raise CatalogError("неверный артикул")
            key = normalize(name)
            if key in seen_names or (sku and code_key(sku) in seen_sku):
                raise CatalogError("повтор названия или артикула")
            product = Product(name, parse_price(cells.get("price", "")), int(raw_stock), sku)
            spec = parse_spec(name)
            if not spec.model and not sku:
                raise CatalogError("нужна конкретная модель или артикул")
            seen_names.add(key)
            if sku:
                seen_sku.add(code_key(sku))
            products.append(product)
        except CatalogError as exc:
            raise CatalogError(f"Строка {index}: {exc}") from exc
    if not products or len(products) > 3000:
        raise CatalogError("нужно от 1 до 3000 позиций")
    return products


def colors_match(request: Spec, item: Spec) -> bool:
    if not request.colors:
        return True
    if request.color_options:
        return bool(request.colors.intersection(item.colors))
    return request.colors.issubset(item.colors)


def attribute_reason(request: Spec, item: Spec) -> str | None:
    checks = (
        (bool(item.issues), 'неоднозначные характеристики в прайсе'),
        (request.brand is not None and request.brand != item.brand, 'бренд'),
        (request.product_line is not None and request.product_line != item.product_line, 'линейка устройств'),
        (not request.role_colors.issubset(item.role_colors), 'цвет оправы / линз'),
        (request.category != item.category, 'категория товара'),
        (request.storage and not request.storage.intersection(item.storage), 'память'),
        (request.ram is not None and request.ram != item.ram, 'RAM'),
        (not colors_match(request, item), 'цвет'),
        (request.sim and request.sim != item.sim and not (request.sim == 'sim' and item.sim == 'sim_esim'), 'SIM'),
        (request.condition and request.condition != item.condition, 'состояние'),
        (not request.condition and item.condition in {'used', 'refurbished', 'damaged'}, 'б/у, восстановленный или повреждённый товар'),
        (request.size and request.size != item.size, 'размер'),
        (request.nfc is not None and request.nfc != item.nfc, 'NFC'),
        (request.connectivity and request.connectivity != item.connectivity, 'Wi-Fi / сотовая связь'),
    )
    return next((label for failed, label in checks if failed), None)


def model_reason(request: Spec, item: Spec) -> str | None:
    if request.brand is not None and request.brand != item.brand:
        return 'бренд'
    if request.product_line is not None and request.product_line != item.product_line:
        return 'линейка устройств'
    if not request.model or request.family != item.family:
        return 'модель'
    if request.family == 'generic':
        if not request.model.issubset(item.model):
            return 'модель'
        # Extra generation/model numbers are never implicit alternatives.
        requested_numbers = {t for t in request.model if re.fullmatch(r'\d+|[ivx]+', t)}
        item_numbers = {t for t in item.model if re.fullmatch(r'\d+|[ivx]+', t)}
        # In camera listings, a trailing 360 describes the recording mode;
        # it is not another generation when a specific model was requested.
        if (item.brand in {'insta360', 'gopro'} and request.model
                and '360' not in request.model
                and any(t != '360' and re.search(r'\d', t) for t in request.model)):
            item_numbers.discard('360')
        if requested_numbers != item_numbers:
            return 'поколение / номер модели'
    elif request.model != item.model:
        return 'модель'
    if request.variants != item.variants:
        return 'версия модели'
    if not request.traits.issubset(item.traits):
        return 'дополнительные признаки'
    return None


def matches(request: Spec, item: Spec) -> bool:
    return not request.issues and not model_reason(request, item) and not attribute_reason(request, item)


@dataclass
class MatchReport:
    classification: str
    products: list[Product]
    details: list[str]


def describe_spec(spec: Spec) -> str:
    family = {'iphone': 'iPhone', 'ipad': 'iPad', 'pixel': 'Pixel', 'samsung': 'Samsung'}.get(spec.family, '')
    versions = [v for v in ('pro', 'max', 'plus', 'ultra', 'mini', 'lite', 'air', 'edge', 'se', 'xl', 'fold', 'fe')
                if v in spec.variants]
    model = ' '.join(filter(None, [family, *sorted(spec.model), *versions]))
    parts = ['модель: ' + (model or 'не распознана')]
    if spec.brand:
        parts.append('бренд: ' + spec.brand)
    if spec.product_line and spec.product_line != spec.brand:
        parts.append('линейка: ' + spec.product_line)
    storage = '/'.join(map(str, sorted(spec.storage))) + ' ГБ' if spec.storage else ''
    sim = {'sim': 'SIM', 'esim': 'eSIM', 'sim_esim': 'SIM+eSIM', 'dual_sim': '2 SIM'}.get(spec.sim)
    for label, value in (('память', storage), ('RAM', f'{spec.ram} ГБ' if spec.ram is not None else ''),
                         ('цвет', '/'.join(sorted(spec.colors))), ('SIM', sim),
                         ('связь', spec.connectivity), ('состояние', spec.condition), ('размер', spec.size)):
        if value:
            parts.append(f'{label}: {value}')
    if spec.nfc is not None:
        parts.append('NFC: ' + ('да' if spec.nfc else 'нет'))
    if spec.max_price is not None:
        parts.append('бюджет: ' + money(spec.max_price))
    return '; '.join(parts)


def product_identifiers(product: Product) -> dict[str, set[str]]:
    """Only complete supplier codes; never treat model generations or memory as SKUs."""
    identifiers = {code_key(product.sku): set(re.findall(r'[a-zа-яё0-9]+', product.sku.casefold()))} if product.sku else {}
    raw_name = unicodedata.normalize('NFKC', product.name).casefold()
    for token in re.findall(r'[a-z0-9]+(?:[-/][a-z0-9]+)*', raw_name):
        if ((re.match(r'[a-z]{2,}', token) and re.search(r'\d{3,}', token))
                or re.fullmatch(r'\d{3}-\d{5}-\d{2}', token)):
            identifiers[code_key(token)] = set(re.findall(r'[a-z0-9]+', token))
    return identifiers


RAYBAN_LINES = frozenset({'wayfarer', 'skyler', 'headliner', 'scriber', 'blayzer'})


def rayban_broad_request(spec: Spec) -> bool:
    """Allow a Ray-Ban range or a known frame line without inventing a model."""
    if spec.brand != 'rayban' or spec.family != 'generic' or spec.category != 'device':
        return False
    return all(token in RAYBAN_LINES or re.fullmatch(r'\d+|s\d{2}', token)
               for token in spec.model)


def rayban_broad_reason(request: Spec, item: Spec, clean: str, name: str) -> str | None:
    if item.brand != 'rayban' or item.family != 'generic':
        return 'бренд'
    # Restrict this broad search to glasses, excluding compatible accessories.
    if not item.model.intersection(RAYBAN_LINES):
        return 'линейка очков'
    if 'meta' in words(clean) and 'meta' not in words(name):
        return 'Meta'
    if not request.model.issubset(item.model):
        return 'линейка / поколение / размер'
    if request.variants and request.variants != item.variants:
        return 'версия модели'
    return None


def broad_brand_reference(clean: str) -> str | None:
    """A single known brand/family can request its available assortment."""
    tokens = words(clean)
    return tokens[0] if len(tokens) == 1 and tokens[0] in BRAND_OWNERS else None


def broad_brand_reason(reference: str, request: Spec, item: Spec, name: str) -> str | None:
    if reference == 'meta':
        return None if 'meta' in words(name) else 'бренд'
    if request.brand != item.brand:
        return 'бренд'
    # Manufacturer names and product families are different: an iPhone query
    # must never include Apple chargers, iPads or unrelated brands.
    if reference == 'iphone':
        return None if item.family == 'iphone' else 'линейка устройств'
    if reference == 'pixel':
        return None if re.search(r'\bpixel\s+\d+[a-z]?\b', normalize(name)) else 'линейка устройств'
    if reference != BRAND_OWNERS[reference] and reference not in words(name):
        return 'линейка устройств'
    return None


def clean_request(text: str) -> str:
    """Remove only conversational wrappers, explicit quantity and memory prepositions."""
    text = BUY.sub(' ', text)
    text = re.sub(r'\b(?:добрый\s+день|доброе\s+утро|добрый\s+вечер|здравствуйте|'
                  r'привет|ребята|друзья|подскажите|подскажи|скажите|пожалуйста|срочно|мне|нам)\b', ' ', text)
    text = re.sub(r'\bна\s+(?=\d{2,4}(?:gb|tb)?\b)', ' ', text)
    text = re.sub(r'(?<![\w/-])\d{1,4}\s*(?:шт(?:ук[аи]?)?\.?|штук(?:а|и)?)(?![\w/-])', ' ', text)
    text = re.sub(r'\b(?:есть|в\s+наличии)\s*\?\s*$', ' ', text)
    return text.strip(' :,-')


def sony_xm_reason(request: Spec, item: Spec, name: str) -> str | None:
    """A short XM6 query can list both WH and WF, with full names and prices."""
    token = next(iter(request.model))
    if item.brand != 'sony' or item.family != 'generic':
        return 'бренд'
    if not re.search(r'\b(?:wh|wf)[- ]?(?:1000)?' + re.escape(token) + r'\b', normalize(name)):
        return 'модель'
    if request.variants and request.variants != item.variants:
        return 'версия модели'
    return None


def inspect_request(text: str, products: list[Product], allow_bare: bool = True) -> MatchReport:
    classification = intent(text, allow_bare)
    report = MatchReport(classification, [], [])
    if classification not in {'buy', 'bare'}:
        report.details.append('Нет однозначного запроса на покупку; ответ отключён.')
        return report
    identifiers = {p: product_identifiers(p) for p in products}
    for segment in request_segments(text):
        request_words = {code_key(token) for token in IDENTIFIER_TOKEN.findall(segment)
                         if not semantic_reference(token)}
        requested_skus = {code for codes in identifiers.values() for code in codes if code in request_words}
        # Attributes encoded inside a supplier ID are not an explicit request
        # for that condition, brand, color or version. Words next to it remain.
        specification_text = IDENTIFIER_TOKEN.sub(
            lambda match: identifier_specification(match[0]) if code_key(match[0]) in requested_skus else match[0], segment)
        normalized, normalization_notes = normalize_with_notes(specification_text)
        clean = clean_request(normalized)
        if normalization_notes:
            report.details.append('Нормализация: ' + '; '.join(normalization_notes))
        request = parse_spec(clean)
        rayban_range = not requested_skus and rayban_broad_request(request)
        brand_reference = broad_brand_reference(clean) if not requested_skus and not rayban_range else None
        sony_xm = (not requested_skus and request.brand == 'sony' and request.family == 'generic'
                   and len(request.model) == 1 and bool(re.fullmatch(r'xm\d+', next(iter(request.model)))))
        if requested_skus:
            report.details.append('Точный артикул: ' + ', '.join(sorted(requested_skus)))
        report.details.append(describe_spec(request))
        issues = list(request.issues)
        # A brand, category or series alone cannot identify a concrete product.
        named_model = (len(request.model) >= 2 or (request.brand is not None and bool(request.model))) and any(
            request.model.issubset(parse_spec(p.name).model)
            and (request.brand is None or request.brand == parse_spec(p.name).brand)
            and request.category == parse_spec(p.name).category
            and request.variants == parse_spec(p.name).variants
            for p in products)
        if not requested_skus and not rayban_range and not brand_reference and (not request.model or (request.family == 'generic' and not named_model and not any(
                re.search(r'\d', t) or re.fullmatch(r'[ivx]+', t) for t in request.model))):
            issues.append('не указана конкретная модель или артикул')
        if request.family == 'iphone' and 'iphone' not in words(clean) and not requested_skus:
            number = next(iter(request.model), '')
            for product in products:
                item = parse_spec(product.name)
                same_number = any(number in re.findall(r'\d+', token) for token in item.model)
                if (product.eligible and item.family != 'iphone' and same_number
                        and request.variants == item.variants and not attribute_reason(replace(request, brand=None), item)
                        and request.traits.issubset(item.traits | item.model)
                        and (request.max_price is None or (product.price is not None and product.price <= request.max_price))):
                    issues.append('короткая модель подходит разным семействам; укажите iPhone или другое название')
                    break
        if issues:
            report.details.append('Пропущено: ' + '; '.join(issues))
            continue
        rejected: dict[str, int] = {}
        candidates: list[Product] = []
        for product in products:
            item = parse_spec(product.name)
            matched_codes = requested_skus.intersection(identifiers[product])
            sku_match = bool(matched_codes)
            reason = None
            if requested_skus and not sku_match:
                reason = 'артикул'
            elif sku_match:
                if request.family != 'generic':
                    reason = model_reason(request, item)
                else:
                    extra = request.model
                    if not extra.issubset(item.model | item.traits | {item.family}):
                        reason = 'признаки рядом с артикулом'
                    elif request.variants and request.variants != item.variants:
                        reason = 'версия модели'
            elif rayban_range:
                reason = rayban_broad_reason(request, item, clean, product.name)
            elif brand_reference:
                reason = broad_brand_reason(brand_reference, request, item, product.name)
            elif sony_xm:
                reason = sony_xm_reason(request, item, product.name)
            else:
                reason = model_reason(request, item)
            # Only explain attribute failures for the requested model.
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            attribute_request = request
            if brand_reference == 'meta':
                attribute_request = replace(request, brand=None)
            if (sku_match and request.family == 'generic' and request.category == 'device' and not extra
                    and not re.search(r'\b(?:телефон|смартфон|планшет|ноутбук)\b', clean)):
                attribute_request = replace(request, category=item.category)
            reason = attribute_reason(attribute_request, item)
            regions = set(re.findall(r'[\U0001F1E6-\U0001F1FF]{2}', segment))
            if not reason and regions and not regions.intersection({product.country, product.price_country}):
                reason = 'регион'
            if not reason and not product.eligible:
                reason = 'остаток / минимальный заказ / предупреждение прайса'
            if not reason and request.max_price is not None and (product.price is None or product.price > request.max_price):
                reason = 'бюджет или неизвестная цена'
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            candidates.append(product)
        candidate_identities = {(parse_spec(p.name).brand, parse_spec(p.name).product_line) for p in candidates}
        if request.category == 'device' and request.brand is None and len(candidate_identities) > 1:
            report.details.append('Пропущено: название подходит нескольким брендам; укажите бренд.')
            candidates = []
        for product in candidates:
            if product not in report.products:
                report.products.append(product)
        report.details.append(f'Подходящих позиций: {len(candidates)}.')
        if rejected:
            report.details.append('Отсеяно: ' + ', '.join(f'{k} — {v}' for k, v in rejected.items()))
    return report


def find_products(text: str, products: list[Product], allow_bare: bool = True) -> tuple[str, list[Product]]:
    report = inspect_request(text, products, allow_bare)
    return report.classification, report.products


def money(price: Decimal) -> str:
    return f"{price:,.2f}".replace(",", " ").removesuffix(".00").replace(".", ",") + " ₽"


_DISPLAY_PRODUCT_CODES = re.compile(
    r'(?<![\w])(?:SM-[A-Z0-9]+(?:/[A-Z0-9]+)?|RW\d{4}[A-Z]?|'
    r'00W\d{4}[A-Z]?|VK[JP]-[A-Z0-9]+|CPH\d{4}|A\d{4}[A-Z]?|'
    r'NP\d{3}[A-Z0-9]+(?:\s*-\s*[A-Z]{2}\d[A-Z]{2})?|'
    r'\d{3}-\d{5}-\d{2})(?![\w])', re.IGNORECASE)


def display_product_name(name: str) -> str:
    """Hide recognizable manufacturer codes only when rendering an offer."""
    cleaned = _DISPLAY_PRODUCT_CODES.sub('', name)
    return re.sub(r'\s+', ' ', cleaned).strip() or name


def build_reply(products: list[Product], intro: str | None = None, contact: str = "", *,
                include_codes: bool = False) -> str:
    unknown_stock = any(p.stock is None for p in products)
    if intro == 'По актуальному прайсу:' or (unknown_stock and intro == 'Есть в наличии:'):
        intro = ''
    elif intro is None:
        intro = '' if unknown_stock else 'Есть в наличии:'
    lines = [intro] if intro else []
    for product in products:
        price = f" — {money(product.price)}" if product.price is not None else ""
        region = f' {product.country or product.price_country}' if product.country or product.price_country else ''
        quantity = f' (от {product.min_qty} шт)' if product.min_qty > 1 else ''
        name = product.name if include_codes else display_product_name(product.name)
        lines.append(f"• {name}{region}{price}{quantity}")
    if contact:
        lines.extend(("", contact))
    return "\n".join(lines)
