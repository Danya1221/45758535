from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache


COLORS = {
    "black": ("black", "черный", "черная", "черное", "черн"),
    "white": ("white", "белый", "белая", "белое"),
    "silver": ("silver", "серебристый", "серебристая", "серебро"),
    "gray": ("gray", "grey", "серый", "серая"),
    "blue": ("blue", "синий", "синяя", "голубой", "голубая"),
    "navy": ("navy", "темно синий", "темно-синий"),
    "green": ("green", "зеленый", "зеленая"),
    "purple": ("purple", "фиолетовый", "фиолетовая"),
    "lilac": ("lilac", "сиреневый", "сиреневая"),
    "lavender": ("lavender", "лавандовый", "лавандовая"),
    "yellow": ("yellow", "желтый", "желтая"),
    "orange": ("orange", "оранжевый", "оранжевая"),
    "red": ("red", "красный", "красная"),
    "pink": ("pink", "розовый", "розовая"),
    "gold": ("gold", "золотой", "золотая", "золото"),
    "burgundy": ("burgundy", "бордовый", "бордовая"),
    "glacier": ("glacier", "глейсер", "глейшер"),
    "natural": ("natural", "натуральный", "натуральная"),
}
ALIASES = {
    "айфон": "iphone", "айфона": "iphone", "айфоны": "iphone",
    "ифон": "iphone", "апл": "apple", "эпл": "apple",
    "айпад": "ipad", "айпэд": "ipad", "айпада": "ipad",
    "самсунг": "samsung", "галакси": "galaxy",
    "сяоми": "xiaomi", "ксиаоми": "xiaomi", "редми": "redmi",
    "пиксель": "pixel", "гугл": "google",
    "маршал": "marshall", "маршалл": "marshall",
    "эмбертон": "emberton", "райбан": "rayban", "ray ban": "rayban",
    "ray-ban": "rayban", "про": "pro", "макс": "max",
    "плюс": "plus", "ультра": "ultra", "мини": "mini",
    "е сим": "esim", "е-сим": "esim", "еsim": "esim",
    "есим": "esim", "сим": "sim", "e-sim": "esim",
    "фотохром": "photochromic", "фотохромные": "photochromic",
    "новый": "new", "новая": "new", "новые": "new",
    "б/у": "used", "бу": "used", "б у": "used",
    "восстановленный": "refurbished", "реф": "refurbished",
}
VARIANTS = frozenset("pro max plus ultra mini lite air edge se xl fold fe".split())
FILLER = frozenset(
    "куплю купим ищу ищем нужен нужна нужно нужны интересует возьму предложите "
    "предлагайте пожалуйста у кого есть кто продаст wtb apple samsung galaxy "
    "google телефон смартфон колонка очки размер size цвет color память storage "
    "gb гб tb тб ram озу sim esim dual nano нано и или or gen поколение "
    "wi fi wifi cellular a brand new бюджет до за руб р rub в наличии".split()
)
BUY = re.compile(r"\b(?:куплю|купим|ищу|ищем|нужен|нужна|нужно|нужны|интересует|возьму|предложите|предлагайте|wtb)\b|у\s+кого\s+есть|кто\s+продаст")
SELL = re.compile(r"\b(?:продам|продаю|продаем|продается|продажа|отдам|wts)\b")
CHATTER = re.compile(r"\b(?:купил|купила|обзор|сломался|сломал|спасибо|почему|настроить|обсуждаем)\b|у\s+меня")
MONEY = re.compile(r"\d\s*(?:₽|руб\b|rub\b|р\.)", re.I)


@lru_cache(maxsize=4096)
def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r"\b(айфон|ифон|айпад|айпэд)(?=\d)", r"\1 ", value)
    for source, target in sorted(ALIASES.items(), key=lambda pair: -len(pair[0])):
        value = re.sub(r"(?<!\w)" + re.escape(source) + r"(?!\w)", target, value)
    value = re.sub(r"\b(iphone|ipad|phone)\s*(?=\d)", r"\1 ", value)
    value = re.sub(r"\b(gen|watch|flip|fold)(?=\d)", r"\1 ", value)
    value = re.sub(r"\b(\d{1,2})\s*pm\b", r"\1 pro max", value)
    value = re.sub(r"\b([sa]\d{2})\s*\+", r"\1 plus", value)
    value = re.sub(r"\b(\d+)\s*(?:тб|tb)\b", r"\1tb", value)
    value = re.sub(r"\b(\d+)\s*(?:гб|gb)\b", r"\1gb", value)
    return re.sub(r"\s+", " ", value).strip()


def words(value: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", normalize(value))


def code_key(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", normalize(value))


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


@lru_cache(maxsize=4096)
def parse_spec(text: str) -> Spec:
    n = normalize(text)
    tokens = set(words(n))
    colors = set()
    for color, aliases in COLORS.items():
        for alias in aliases:
            if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", n):
                colors.add(color)
                n = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", " ", n)
    storage = set()
    for count, unit in re.findall(r"\b(\d{1,4})(gb|tb)\b", n):
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
        if re.search(rf"\b{capacity}\b", n):
            storage.add(capacity)
    sim = None
    if re.search(r"\b(?:dual\s*sim|2\s*sim|две\s*sim)\b", n):
        sim = "dual_sim"
    elif re.search(r"(?<!\w)sim(?!\w)", n):
        sim = "sim"
    elif re.search(r"(?<!\w)esim(?!\w)", n):
        sim = "esim"
    variant = tokens & VARIANTS
    family = "generic"
    model: set[str] = set()
    iphone = re.search(r"\biphone\s+(\d{1,2})\b", n)
    short_iphone = re.search(r"^\s*(\d{1,2})\b", n)
    if iphone:
        family, model = "iphone", {iphone[1]}
    elif short_iphone and (variant or storage) and 6 <= int(short_iphone[1]) <= 30:
        family, model = "iphone", {short_iphone[1]}
    elif "iphone" in tokens:
        family = "iphone"
    else:
        not_samsung_phone = re.search(r'\b(?:galaxy|samsung)\s+(?:tab|watch|buds|ring|book)\b', n) or bool(
            tokens & {'rayban', 'meta', 'oakley', 'sony', 'gopro', 'instax', 'fujifilm', 'garmin', 'coros', 'oura'})
        samsung = None if not_samsung_phone else re.search(r"\b([sa]\d{2}[a-z]?)\b", n)
        numbered = re.search(r"\b(ipad|pixel)\s+(\d{1,2})\b", n)
        if samsung:
            family, model = "samsung", {samsung[1]}
        elif numbered:
            family, model = numbered[1], {numbered[2]}
    cleaned = re.sub(r"\b\d{1,4}(?:gb|tb)\b", " ", n)
    cleaned = re.sub(r"\b\d{1,2}\s*/\s*\d{1,4}\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:размер|size)\s+[xsml]{1,3}\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:до|бюджет)\s*[:=]?\s*[\d ]+(?:₽|руб|rub)?", " ", cleaned)
    traits = {
        t for t in words(cleaned)
        if t not in FILLER and t not in VARIANTS and t not in {str(s) for s in storage}
        and (ram is None or t != str(ram))
        and t not in {"new", "used", "refurbished"}
    }
    if sim == "dual_sim":
        traits.discard("2")
    if family == "generic":
        model = traits
        if "rayban" in model:
            model -= {"m", "s", "l", "xl"}
        traits = set()
    else:
        traits -= model | {family}
    size_match = re.search(r"\b(?:размер|size)\s*([xsml]{1,3})\b", n)
    if not size_match and "rayban" in tokens:
        size_match = re.search(r"\b([sml]|xl|xxl)\s*$", n)
    condition = next((x for x in ("refurbished", "used", "new") if x in tokens), None)
    budget = re.search(r"\b(?:до|бюджет)\s*[:=]?\s*(\d[\d ]{2,9})(?:₽|руб|rub|$)", normalize(text))
    max_price = Decimal(budget[1].replace(" ", "")) if budget else None
    return Spec(family, frozenset(model), frozenset(variant), frozenset(storage), ram,
                frozenset(colors), sim, condition, size_match[1] if size_match else None, frozenset(traits),
                bool(re.search(r'\b(?:или|or)\b', normalize(text))), max_price)


def intent(text: str, allow_bare: bool) -> str:
    n = normalize(text)
    if SELL.search(n):
        return "sell"
    if re.search(r"\b(?:не|кроме|без)\b", n):
        return "uncertain"
    # Attribute negations are deliberately not guessed.
    if re.search(r"\b(?:не|кроме|без)\s+(?:black|white|silver|esim|sim|черн\w*|бел\w*|серебр\w*)", n):
        return "uncertain"
    if BUY.search(n):
        return "buy"
    if re.search(r"\b(?:в наличии|прайс|оптом|распродажа)\b", n) or MONEY.search(n):
        return "sell"
    trailing_price = re.search(r"\b(\d{4,7})(?:[,.]\d{1,2})?\s*$", n)
    if trailing_price and int(trailing_price[1]) not in {1024, 2048}:
        return "sell"
    if allow_bare and not CHATTER.search(n) and len(words(n)) <= 24:
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


def matches(request: Spec, item: Spec) -> bool:
    if not request.model or request.family != item.family:
        return False
    if request.family == "generic":
        if not request.model.issubset(item.model):
            return False
    elif request.model != item.model:
        return False
    if request.variants != item.variants:
        return False
    if not request.traits.issubset(item.traits):
        return False
    return not (
        (request.storage and not request.storage.intersection(item.storage))
        or (request.ram is not None and request.ram != item.ram)
        or not colors_match(request, item)
        or (request.sim and request.sim != item.sim)
        or (request.condition and request.condition != item.condition)
        or (request.size and request.size != item.size)
    )


def find_products(text: str, products: list[Product], allow_bare: bool = True) -> tuple[str, list[Product]]:
    classification = intent(text, allow_bare)
    if classification not in {"buy", "bare"}:
        return classification, []
    result: list[Product] = []
    for segment in request_segments(text):
        # Strip intent words from the start so short names like "18 pro max" work.
        clean = BUY.sub(" ", normalize(segment)).strip(" :,-")
        request = parse_spec(clean)
        request_words = {code_key(t) for t in re.findall(r"[\w-]+", clean)}
        for product in products:
            if not product.eligible:
                continue
            requested_regions = set(re.findall(r'[\U0001F1E6-\U0001F1FF]{2}', segment))
            if requested_regions and not requested_regions.intersection({product.country, product.price_country}):
                continue
            item = parse_spec(product.name)
            sku_match = product.sku and code_key(product.sku) in request_words
            if not (sku_match or matches(request, item)):
                continue
            if sku_match:
                if request.family != 'generic' and (request.family != item.family or request.model != item.model or request.variants != item.variants):
                    continue
                if request.variants and request.variants != item.variants:
                    continue
                if request.family == 'generic':
                    extra = request.model - set(words(product.sku))
                    if not extra.issubset(item.model | item.traits | {item.family}):
                        continue
            # A SKU does not override explicit contradictory characteristics.
            if sku_match and any((request.storage, request.ram, request.colors, request.sim, request.condition, request.size)):
                compatible = not (
                    (request.storage and not request.storage.intersection(item.storage))
                    or (request.ram is not None and request.ram != item.ram)
                    or not colors_match(request, item)
                    or (request.sim and request.sim != item.sim)
                    or (request.condition and request.condition != item.condition)
                    or (request.size and request.size != item.size)
                )
                if not compatible:
                    continue
            if request.max_price is not None and (product.price is None or product.price > request.max_price):
                continue
            if product not in result:
                result.append(product)
    return classification, result


def money(price: Decimal) -> str:
    return f"{price:,.2f}".replace(",", " ").removesuffix(".00").replace(".", ",") + " ₽"


def build_reply(products: list[Product], intro: str | None = None, contact: str = "", limit: int = 5) -> str:
    lines = [intro or ('По актуальному прайсу:' if any(p.stock is None for p in products) else 'Есть в наличии:')]
    for product in products[:limit]:
        price = f" — {money(product.price)}" if product.price is not None else ""
        region = f' {product.country or product.price_country}' if product.country or product.price_country else ''
        quantity = f' (от {product.min_qty} шт)' if product.min_qty > 1 else ''
        lines.append(f"• {product.name}{region}{price}{quantity}")
    if len(products) > limit:
        lines.append(f"Ещё вариантов: {len(products) - limit}.")
    if contact:
        lines.extend(("", contact))
    return "\n".join(lines)
