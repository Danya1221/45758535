from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unicodedata
from decimal import Decimal
from pathlib import Path

from .catalog import Product, product_key, build_reply
from .prices import PriceFeedError
from .pricing import MarkupPolicy


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, price TEXT, stock INTEGER NOT NULL, sku TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supplier_products (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, price TEXT, stock INTEGER NOT NULL,
                sku TEXT NOT NULL, stock_known INTEGER NOT NULL, country TEXT NOT NULL,
                price_country TEXT NOT NULL, min_qty INTEGER NOT NULL, warnings TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS excluded_users (
                user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '', created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL, request_key TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS dedup_requests ON deliveries(sender_id, request_key, created);
            CREATE TABLE IF NOT EXISTS message_checks (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                observed REAL NOT NULL, result TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS price_runs (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, source TEXT NOT NULL,
                complete INTEGER NOT NULL, product_count INTEGER NOT NULL, summary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS price_changes (
                run_id INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
                country TEXT NOT NULL, old_price TEXT, new_price TEXT
            );
            CREATE TABLE IF NOT EXISTS extra_price_items (
                source TEXT NOT NULL, item_key TEXT NOT NULL, name TEXT NOT NULL,
                price TEXT NOT NULL, stock INTEGER, country TEXT NOT NULL,
                warnings TEXT NOT NULL, seen_at REAL NOT NULL,
                PRIMARY KEY(source, item_key)
            );
            CREATE TABLE IF NOT EXISTS quotes (
                quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                request_text TEXT NOT NULL, products TEXT NOT NULL, created REAL NOT NULL,
                UNIQUE(account_id, chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS quotes_by_buyer ON quotes(account_id, sender_id, created);
            CREATE TABLE IF NOT EXISTS quote_messages (
                account_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, quote_id INTEGER NOT NULL,
                PRIMARY KEY(account_id, sender_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS quote_messages_by_quote ON quote_messages(quote_id);
            CREATE TABLE IF NOT EXISTS followups (
                account_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, quote_id INTEGER NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(account_id, sender_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS followups_by_created ON followups(created);
            CREATE TABLE IF NOT EXISTS quote_holds (
                account_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
                quote_id INTEGER NOT NULL, product_key TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(quote_id, product_key)
            );
            CREATE INDEX IF NOT EXISTS quote_holds_by_created ON quote_holds(created);
            CREATE TABLE IF NOT EXISTS stock_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL, quote_id INTEGER NOT NULL, product_key TEXT NOT NULL,
                generation INTEGER NOT NULL, product_id INTEGER NOT NULL, product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hold_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, quote_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '', buyer_name TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL, product_text TEXT NOT NULL DEFAULT '', quoted_price TEXT,
                buyer_text TEXT NOT NULL DEFAULT '', quantity INTEGER,
                stock_status TEXT NOT NULL DEFAULT '', reservation_id INTEGER,
                created REAL NOT NULL,
                UNIQUE(account_id, sender_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS hold_requests_created ON hold_requests(created DESC, id DESC);
        """)
        hold_columns = {r[1] for r in self.db.execute('PRAGMA table_info(hold_requests)')}
        if 'quoted_price' not in hold_columns:
            self.db.execute('ALTER TABLE hold_requests ADD COLUMN quoted_price TEXT')
        quote_columns = {r[1] for r in self.db.execute('PRAGMA table_info(quotes)')}
        if 'catalog_generation' not in quote_columns:
            self.db.execute('ALTER TABLE quotes ADD COLUMN catalog_generation INTEGER')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(products)')}
        for column, definition in [('stock_known', 'INTEGER NOT NULL DEFAULT 1'), ('country', "TEXT NOT NULL DEFAULT ''"),
                                   ('price_country', "TEXT NOT NULL DEFAULT ''"), ('min_qty', 'INTEGER NOT NULL DEFAULT 1'),
                                   ('warnings', "TEXT NOT NULL DEFAULT '[]'")]:
            if column not in columns:
                self.db.execute(f'ALTER TABLE products ADD COLUMN {column} {definition}')
        # Older deployments held either the supplier feed or the owner's list
        # in the same table. Move only an explicitly identified supplier feed.
        if not self.get('source_split_v1', False):
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if self.get('price_source') is not None:
                    self.db.execute('''INSERT INTO supplier_products
                        (name,price,stock,sku,stock_known,country,price_country,min_qty,warnings)
                        SELECT name,price,stock,sku,stock_known,country,price_country,min_qty,warnings FROM products''')
                    self.db.execute('DELETE FROM products')
                    # Older quotes did not carry their supplier identity.
                    self.set('catalog_generation', self.get('catalog_generation', 0) + 1)
                self.set('source_split_v1', True)
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        path.chmod(0o600)
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('active', 'false')")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('allow_bare', 'true')")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('intro', 'Есть в наличии:')")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('catalog_version', '0')")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('pricing_version', '0')")
        # Remove the former automatic supplier heading from persisted setups.
        # Custom owner-written introductions remain intact.
        if self.get('intro') == 'По актуальному прайсу:':
            self.set('intro', '')
        # An interrupted send must not be retried automatically after a restart.
        if self.db.execute("SELECT 1 FROM deliveries WHERE status='pending'").fetchone():
            self.db.execute("UPDATE deliveries SET status='uncertain', detail='process_restart' WHERE status='pending'")
            self.set('active', False)
        if self.db.execute("SELECT 1 FROM followups WHERE status='pending'").fetchone():
            self.db.execute("UPDATE followups SET status='uncertain', detail='process_restart' WHERE status='pending'")
            self.set('active', False)
        if self.db.execute("SELECT 1 FROM quote_holds WHERE status='pending'").fetchone():
            self.db.execute("UPDATE quote_holds SET status='uncertain', detail='process_restart' WHERE status='pending'")
            self.set('active', False)

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return row[0]

    def set(self, key: str, value):
        self.db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    def _base_products(self) -> list[Product]:
        return [Product(row['name'], Decimal(row['price']) if row['price'] else None,
                        row['stock'] if row['stock_known'] else None, row['sku'], row['country'],
                        row['price_country'], row['min_qty'], tuple(json.loads(row['warnings'])))
                for row in self.db.execute("SELECT * FROM products ORDER BY id")]

    def _supplier_products(self) -> list[Product]:
        return [Product(row['name'], Decimal(row['price']) if row['price'] else None,
                        row['stock'] if row['stock_known'] else None, row['sku'], row['country'],
                        row['price_country'], row['min_qty'], tuple(json.loads(row['warnings'])), 'supplier')
                for row in self.db.execute('SELECT * FROM supplier_products ORDER BY id')]

    def products(self) -> list[Product]:
        products = self._base_products() + self._supplier_products()
        # Spreadsheet prices are only usable for ten minutes after receipt.
        rows = self.db.execute('SELECT * FROM extra_price_items WHERE seen_at >= ? ORDER BY rowid',
                               (time.time() - 600,))
        products.extend(Product(r['name'], Decimal(r['price']), r['stock'], country=r['country'],
                                warnings=tuple(json.loads(r['warnings'])), source=r['source']) for r in rows)
        return products

    def quotable_products(self) -> list[Product]:
        return [p for p in self.products() if
                ((not p.source and self.source_enabled('own')) or
                 (p.source and self.source_enabled('price') and
                  (p.source != 'supplier' or self.get('price_ready', True))))]

    def source_enabled(self, source: str) -> bool:
        if source not in {'own', 'price'}:
            raise ValueError('Unknown source')
        return self.get('answers_source_' + source, True) is not False

    def toggle_answer_source(self, source: str) -> bool:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            enabled = not self.source_enabled(source)
            self.set('answers_source_' + source, enabled)
            self.set('catalog_version', self.get('catalog_version', 0) + 1)
            self.db.execute('COMMIT')
            return enabled
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def apply_extra_snapshot(self, snapshot, source: str, message_id: int, observed: float):
        if source != 'supernova' or not snapshot.products or not (time.time() - 120 <= observed <= time.time() + 30):
            raise PriceFeedError('Прайс пуст или получен слишком давно')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            account = self.get('price_account_id') or self.get('telegram_account_id') or 0
            previous_account = self.get('supernova_account_id', account)
            if account == previous_account and message_id <= self.get('supernova_message_id', 0):
                self.db.execute('ROLLBACK')
                return False
            self.db.execute('UPDATE extra_price_items SET seen_at=0 WHERE source=?', (source,))
            for p in snapshot.products:
                self.db.execute('''INSERT INTO extra_price_items(source,item_key,name,price,stock,country,warnings,seen_at)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source,item_key) DO UPDATE SET
                    name=excluded.name,price=excluded.price,stock=excluded.stock,country=excluded.country,
                    warnings=excluded.warnings,seen_at=excluded.seen_at''',
                    (source, product_key(p), p.name, str(p.price), p.stock, p.country,
                     json.dumps(p.warnings), observed))
            # Rows absent in this unverified export stay stored but cannot be offered.
            self.set('supernova_message_id', message_id)
            self.set('supernova_account_id', account)
            self.set('supernova_updated_at', observed)
            self.set('catalog_version', self.get('catalog_version', 0) + 1)
            self.db.execute('COMMIT')
            return True
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def markup_policy(self) -> MarkupPolicy:
        return MarkupPolicy.from_data(self.get('markup_rules'))

    def set_markup_policy(self, policy: MarkupPolicy):
        # Revalidate the serialized snapshot before replacing persisted rules.
        validated = MarkupPolicy.from_data(policy.to_data())
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.set('markup_rules', validated.to_data())
            self.set('pricing_version', self.get('pricing_version', 0) + 1)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _write_products(self, products):
        self.db.execute('DELETE FROM products')
        self.db.executemany('INSERT INTO products(name,price,stock,sku,stock_known,country,price_country,min_qty,warnings) VALUES (?,?,?,?,?,?,?,?,?)',
                            [(p.name, str(p.price) if p.price is not None else None, p.stock if p.stock is not None else 1,
                              p.sku, int(p.stock is not None), p.country, p.price_country, p.min_qty, json.dumps(p.warnings)) for p in products])
        self.set('catalog_version', self.get('catalog_version', 0) + 1)
        self.set('catalog_generation', self.get('catalog_generation', 0) + 1)

    def replace_products(self, products: list[Product]):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._write_products(products)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def apply_price_snapshot(self, snapshot, source: str) -> dict:
        previous_source = self.get('price_source')
        if not snapshot.complete and previous_source != source and self._supplier_products():
            raise PriceFeedError('Для смены источника нужен полный прайс с итоговым количеством товаров')
        old = {product_key(p): p for p in self._supplier_products()} if previous_source == source else {}
        incoming = {product_key(p): p for p in snapshot.products}
        resulting = incoming if snapshot.complete else old | incoming
        removed = old.keys() - incoming.keys() if snapshot.complete else set()
        changes = []
        for key, product in incoming.items():
            if key not in old:
                changes.append(('added', product, None, product.price))
            elif old[key].price != product.price:
                changes.append(('changed', product, old[key].price, product.price))
        for key in removed:
            changes.append(('removed', old[key], old[key].price, None))
        summary = {kind: sum(c[0] == kind for c in changes) for kind in ('added', 'changed', 'removed')}
        summary.update(total=len(resulting), duplicates=snapshot.duplicates,
                       warnings=sum(bool(p.warnings) for p in incoming.values()), baseline=previous_source != source,
                       complete=snapshot.complete)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            cursor = self.db.execute('INSERT INTO price_runs(created,source,complete,product_count,summary) VALUES (?,?,?,?,?)',
                                     (time.time(), source, int(snapshot.complete), len(resulting), json.dumps(summary)))
            self.db.executemany('INSERT INTO price_changes(run_id,kind,name,country,old_price,new_price) VALUES (?,?,?,?,?,?)',
                                [(cursor.lastrowid, kind, p.name, p.country or p.price_country,
                                  str(old_price) if old_price is not None else None,
                                  str(new_price) if new_price is not None else None) for kind, p, old_price, new_price in changes])
            self.db.execute('DELETE FROM supplier_products')
            self.db.executemany('''INSERT INTO supplier_products
                (name,price,stock,sku,stock_known,country,price_country,min_qty,warnings) VALUES (?,?,?,?,?,?,?,?,?)''',
                [(p.name, str(p.price) if p.price is not None else None, p.stock if p.stock is not None else 1,
                  p.sku, int(p.stock is not None), p.country, p.price_country, p.min_qty, json.dumps(p.warnings))
                 for p in resulting.values()])
            self.set('catalog_version', self.get('catalog_version', 0) + 1)
            self.set('price_source', source)
            self.set('price_updated_at', time.time())
            self.set('price_ready', True)
            # Supplier replies start with the product; a price feed establishes
            # neither stock availability nor a need for an automatic heading.
            if self.get('intro') in {'Есть в наличии:', 'По актуальному прайсу:'}:
                self.set('intro', '')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return summary

    def latest_price_run(self):
        row = self.db.execute('SELECT * FROM price_runs ORDER BY id DESC LIMIT 1').fetchone()
        if not row:
            return None, []
        changes = [dict(c) for c in self.db.execute('SELECT * FROM price_changes WHERE run_id=? LIMIT 50', (row['id'],))]
        return dict(row), changes

    def chats(self) -> dict[int, str]:
        return {row['id']: row['title'] for row in self.db.execute("SELECT * FROM chats ORDER BY title")}

    def exclude_user(self, user_id: int, username: str = '', display_name: str = ''):
        if type(user_id) is not int or user_id <= 0:
            raise ValueError('Expected a Telegram user')
        self.db.execute('''INSERT INTO excluded_users(user_id,username,display_name,created) VALUES (?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name''',
            (user_id, username[:32], display_name[:160], time.time()))

    def is_excluded(self, user_id: int) -> bool:
        return self.db.execute('SELECT 1 FROM excluded_users WHERE user_id=?', (user_id,)).fetchone() is not None

    def unexclude_user(self, user_id: int) -> bool:
        return self.db.execute('DELETE FROM excluded_users WHERE user_id=?', (user_id,)).rowcount == 1

    def excluded_users(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM excluded_users ORDER BY created DESC, user_id')]

    def toggle_chat(self, chat_id: int, title: str) -> bool:
        if chat_id in self.chats():
            self.db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
            return False
        self.db.execute("INSERT INTO chats VALUES (?,?)", (chat_id, title))
        return True

    def reserve(self, chat_id: int, message_id: int, sender_id: int, text: str,
                dedup_seconds: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        # Keep different spellings separate: "poco" and "поко" should both
        # be answerable. Identical cross-posts still share a deduplication key.
        request_text = ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
        key = hashlib.sha256(request_text.encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM deliveries WHERE chat_id=? AND message_id=?", (chat_id, message_id)).fetchone():
                self.db.execute("COMMIT")
                return False
            duplicate = self.db.execute(
                "SELECT 1 FROM deliveries WHERE sender_id=? AND request_key=? AND created>? "
                "AND status IN ('pending','sent','uncertain') LIMIT 1", (sender_id, key, now - dedup_seconds)
            ).fetchone()
            if duplicate:
                self.db.execute("COMMIT")
                return False
            self.db.execute("INSERT INTO deliveries(chat_id,message_id,sender_id,request_key,status,created) VALUES (?,?,?,?,?,?)",
                            (chat_id, message_id, sender_id, key, 'pending', now))
            self.db.execute("COMMIT")
            return True
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def finish(self, chat_id: int, message_id: int, status: str, detail: str = ""):
        self.db.execute("UPDATE deliveries SET status=?,detail=? WHERE chat_id=? AND message_id=?",
                        (status, detail[:120], chat_id, message_id))

    def sent_since(self, since: float) -> int:
        return self.db.execute('''SELECT
            (SELECT COUNT(*) FROM deliveries WHERE status='sent' AND created>?) +
            (SELECT COUNT(*) FROM followups WHERE status='sent' AND detail!='owner_notice' AND created>?)''', (since, since)).fetchone()[0]

    def recent(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT chat_id,message_id,status,detail,created FROM deliveries ORDER BY created DESC LIMIT 10")]

    def delivery(self, chat_id: int, message_id: int):
        row = self.db.execute('SELECT * FROM deliveries WHERE chat_id=? AND message_id=?', (chat_id, message_id)).fetchone()
        return dict(row) if row else None

    def record_check(self, chat_id: int, message_id: int, result: str, detail: str = ''):
        # Keep metadata only: no message text, phone, login code or password.
        self.db.execute('''INSERT INTO message_checks VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
            observed=CASE WHEN message_checks.result='sent' THEN message_checks.observed ELSE excluded.observed END,
            result=CASE WHEN message_checks.result='sent' THEN 'sent' ELSE excluded.result END,
            detail=CASE WHEN message_checks.result='sent' THEN message_checks.detail ELSE excluded.detail END''',
            (chat_id, message_id, time.time(), result, detail[:120]))
        self.db.execute('''DELETE FROM message_checks WHERE rowid IN
            (SELECT rowid FROM message_checks ORDER BY observed DESC, rowid DESC LIMIT -1 OFFSET 300)''')

    def message_check(self, chat_id: int, message_id: int):
        row = self.db.execute('SELECT * FROM message_checks WHERE chat_id=? AND message_id=?', (chat_id, message_id)).fetchone()
        return dict(row) if row else None

    def latest_checks(self, chat_id: int | None = None, *, selected_only: bool = False):
        if selected_only:
            return [dict(row) for row in self.db.execute('''SELECT message_checks.* FROM message_checks
                JOIN chats ON chats.id=message_checks.chat_id
                ORDER BY observed DESC, message_checks.rowid DESC LIMIT 5''')]
        rows = self.db.execute('SELECT * FROM message_checks WHERE chat_id=? ORDER BY observed DESC, rowid DESC LIMIT 5', (chat_id,)) if chat_id is not None else self.db.execute('SELECT * FROM message_checks ORDER BY observed DESC, rowid DESC LIMIT 5')
        return [dict(row) for row in rows]

    def save_quote(self, account_id: int, sender_id: int, chat_id: int, message_id: int,
                   request_text: str, products: list[Product], now: float | None = None,
                   sent_message_ids=(), catalog_generation=None) -> int:
        """Persist the actual quoted prices, never a reference to the live catalog."""
        if account_id <= 0 or sender_id <= 0 or not products:
            raise ValueError('A quote requires an account, buyer and products')
        now = time.time() if now is None else now
        if catalog_generation is None:
            catalog_generation = self.get('catalog_generation', 0)
        serialized = json.dumps([
            {'name': p.name, 'price': str(p.price) if p.price is not None else None,
             'stock': p.stock, 'sku': p.sku, 'country': p.country,
             'price_country': p.price_country, 'min_qty': p.min_qty, 'warnings': p.warnings, 'source': p.source}
            for p in products
        ], ensure_ascii=False)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._prune_quote_context(now)
            self.db.execute('''INSERT OR IGNORE INTO quotes
                (account_id,sender_id,chat_id,message_id,request_text,products,created,catalog_generation)
                VALUES (?,?,?,?,?,?,?,?)''',
                (account_id, sender_id, chat_id, message_id, request_text, serialized, now, catalog_generation))
            row = self.db.execute('''SELECT quote_id,sender_id FROM quotes
                WHERE account_id=? AND chat_id=? AND message_id=?''',
                (account_id, chat_id, message_id)).fetchone()
            if row['sender_id'] != sender_id:
                raise ValueError('The source request belongs to another buyer')
            quote_id = row['quote_id']
            self.db.executemany('''INSERT OR IGNORE INTO quote_messages
                (account_id,sender_id,message_id,quote_id) VALUES (?,?,?,?)''',
                [(account_id, sender_id, sent_id, quote_id) for sent_id in (sent_message_ids or ())])
            self.db.execute('COMMIT')
            return quote_id
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _read_quote(self, row):
        if row is None:
            return None
        quote = dict(row)
        quote['products'] = [Product(p['name'], Decimal(p['price']) if p['price'] is not None else None,
                                     p['stock'], p['sku'], p['country'], p['price_country'],
                                     p['min_qty'], tuple(p['warnings']), p.get('source', '')) for p in json.loads(row['products'])]
        quote['sent_message_ids'] = [r[0] for r in self.db.execute(
            'SELECT message_id FROM quote_messages WHERE quote_id=? ORDER BY message_id', (row['quote_id'],))]
        return quote

    @staticmethod
    def _quote_cutoff(now: float | None, max_age: float):
        now = time.time() if now is None else now
        if max_age < 0:
            raise ValueError('max_age must not be negative')
        return now, now - min(max_age, 86400)

    def latest_quote(self, account_id: int, sender_id: int, now: float | None = None,
                     max_age: float = 86400):
        now, cutoff = self._quote_cutoff(now, max_age)
        return self._read_quote(self.db.execute('''SELECT * FROM quotes
            WHERE account_id=? AND sender_id=? AND created>=? AND created<=?
            ORDER BY created DESC, quote_id DESC LIMIT 1''', (account_id, sender_id, cutoff, now)).fetchone())

    def quote_for_reply(self, account_id: int, sender_id: int, sent_message_id: int,
                        now: float | None = None, max_age: float = 86400):
        now, cutoff = self._quote_cutoff(now, max_age)
        return self._read_quote(self.db.execute('''SELECT quotes.* FROM quotes JOIN quote_messages
            ON quote_messages.quote_id=quotes.quote_id
            WHERE quote_messages.account_id=? AND quote_messages.sender_id=? AND quote_messages.message_id=?
              AND quotes.account_id=? AND quotes.sender_id=? AND quotes.created>=? AND quotes.created<=?''',
            (account_id, sender_id, sent_message_id, account_id, sender_id, cutoff, now)).fetchone())

    def reserve_followup(self, account_id: int, sender_id: int, message_id: int,
                         quote_id: int, kind: str, now: float | None = None) -> bool:
        if kind not in {'discount', 'hold', 'clarify'}:
            raise ValueError('Invalid follow-up kind')
        now = time.time() if now is None else now
        cursor = self.db.execute('''INSERT OR IGNORE INTO followups
            (account_id,sender_id,message_id,quote_id,kind,status,created)
            SELECT ?,?,?,?,?, 'pending', ? FROM quotes
            WHERE quote_id=? AND account_id=? AND sender_id=? AND created>=? AND created<=?''',
            (account_id, sender_id, message_id, quote_id, kind, now,
             quote_id, account_id, sender_id, now - 86400, now))
        return cursor.rowcount == 1

    def finish_followup(self, account_id: int, sender_id: int, message_id: int,
                        status: str, detail: str = '') -> bool:
        if status not in {'sent', 'failed', 'uncertain'}:
            raise ValueError('Invalid final follow-up status')
        cursor = self.db.execute('''UPDATE followups SET status=?,detail=?
            WHERE account_id=? AND sender_id=? AND message_id=? AND status='pending' ''',
            (status, detail[:120], account_id, sender_id, message_id))
        return cursor.rowcount == 1

    def followup(self, account_id: int, sender_id: int, message_id: int):
        row = self.db.execute('SELECT * FROM followups WHERE account_id=? AND sender_id=? AND message_id=?',
                              (account_id, sender_id, message_id)).fetchone()
        return dict(row) if row else None

    def latest_followup(self, account_id: int, sender_id: int, quote_id: int):
        row = self.db.execute('''SELECT * FROM followups
            WHERE account_id=? AND sender_id=? AND quote_id=?
            ORDER BY created DESC, message_id DESC LIMIT 1''', (account_id, sender_id, quote_id)).fetchone()
        return dict(row) if row else None

    def reserve_hold(self, account_id: int, sender_id: int, quote_id: int,
                     products: list[Product], now: float | None = None) -> list[Product]:
        """Reserve one owner notification per quoted offer, without changing stock."""
        now = time.time() if now is None else now
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('''SELECT * FROM quotes WHERE quote_id=? AND account_id=? AND sender_id=?
                AND created>=? AND created<=?''', (quote_id, account_id, sender_id, now - 86400, now)).fetchone()
            quote = self._read_quote(row)
            snapshot = {self._hold_key(p): p for p in quote['products']} if quote else {}
            reserved = []
            for product in products:
                key = self._hold_key(product)
                if key not in snapshot or product != snapshot[key]:
                    continue
                cursor = self.db.execute('''INSERT OR IGNORE INTO quote_holds
                    (account_id,sender_id,quote_id,product_key,status,created)
                    VALUES (?,?,?,?,'pending',?)''', (account_id, sender_id, quote_id, key, now))
                if cursor.rowcount == 1:
                    reserved.append(snapshot[key])
            self.db.execute('COMMIT')
            return reserved
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def reserve_stock_followup(self, account_id, sender_id, message_id, quote_id, product, quantity, now,
                               *, username='', buyer_name='', buyer_text=''):
        """Record the request and inventory change together, so replay cannot deduct twice."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if not self.reserve_followup(account_id, sender_id, message_id, quote_id, 'hold', now=now):
                self.db.execute('COMMIT')
                return {'status': 'duplicate'}
            row = self.db.execute('''SELECT * FROM quotes WHERE quote_id=? AND account_id=? AND sender_id=?
                AND created>=? AND created<=?''', (quote_id, account_id, sender_id, now - 86400, now)).fetchone()
            quote = self._read_quote(row)
            if not quote or product not in quote['products']:
                raise ValueError('Product does not belong to quote')
            key = self._hold_key(product)
            cursor = self.db.execute('''INSERT OR IGNORE INTO quote_holds
                (account_id,sender_id,quote_id,product_key,status,created)
                VALUES (?,?,?,?,'pending',?)''', (account_id, sender_id, quote_id, key, now))
            if not cursor.rowcount:
                self.db.execute('COMMIT')
                return {'status': 'duplicate_hold'}
            generation = self.get('catalog_generation', 0)
            result = {'status': 'unknown'}
            identity = json.dumps([product_key(product), product.sku], ensure_ascii=False, separators=(',', ':'))
            if quote['catalog_generation'] is None or quote['catalog_generation'] != generation:
                result = {'status': 'changed'}
            elif product.source:
                result = {'status': 'supplier'}
            elif quantity is None:
                result = {'status': 'quantity'}
            elif self.db.execute('''SELECT 1 FROM stock_reservations WHERE account_id=? AND sender_id=?
                    AND product_key=? AND generation=? AND active=1''',
                    (account_id, sender_id, identity, generation)).fetchone():
                result = {'status': 'already'}
            else:
                rows = self.db.execute('''SELECT * FROM products WHERE name=? AND sku=? AND country=?
                    AND price_country=? AND min_qty=?''', (product.name, product.sku, product.country,
                    product.price_country, product.min_qty)).fetchall()
                if len(rows) != 1:
                    result = {'status': 'ambiguous'}
                elif not rows[0]['stock_known']:
                    result = {'status': 'unknown'}
                elif rows[0]['stock'] < quantity:
                    result = {'status': 'insufficient', 'remaining': rows[0]['stock']}
                else:
                    item = rows[0]
                    self.db.execute('UPDATE products SET stock=stock-? WHERE id=?', (quantity, item['id']))
                    self.set('catalog_version', self.get('catalog_version', 0) + 1)
                    reserved = self.db.execute('''INSERT INTO stock_reservations
                        (account_id,sender_id,quote_id,product_key,generation,product_id,product_name,quantity,created)
                        VALUES (?,?,?,?,?,?,?,?,?)''', (account_id, sender_id, quote_id, identity, generation,
                                                        item['id'], product.name, quantity, now))
                    result = {'status': 'reserved', 'quantity': quantity, 'remaining': item['stock'] - quantity,
                              'reservation_id': reserved.lastrowid}
            if result['status'] != 'already':
                self.db.execute('''INSERT INTO hold_requests
                    (account_id,sender_id,message_id,quote_id,username,buyer_name,kind,product_text,quoted_price,
                     buyer_text,quantity,stock_status,reservation_id,created)
                    VALUES (?,?,?,?,?,?,'hold',?,?,?,?,?,?,?)''',
                    (account_id, sender_id, message_id, quote_id, username[:32], buyer_name[:160],
                     build_reply([product], intro='')[:500], str(product.price) if product.price is not None else None,
                     buyer_text[:500], quantity,
                     result['status'], result.get('reservation_id'), now))
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def reserve_clarification_followup(self, account_id, sender_id, message_id, quote_id, now,
                                       *, username='', buyer_name='', buyer_text=''):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if not self.reserve_followup(account_id, sender_id, message_id, quote_id, 'clarify', now=now):
                self.db.execute('COMMIT')
                return False
            self.db.execute('''INSERT INTO hold_requests
                (account_id,sender_id,message_id,quote_id,username,buyer_name,kind,buyer_text,created)
                VALUES (?,?,?,?,?,?,'clarify',?,?)''',
                (account_id, sender_id, message_id, quote_id, username[:32], buyer_name[:160], buyer_text[:500], now))
            self.db.execute('COMMIT')
            return True
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def hold_requests(self, *, limit=5, offset=0):
        return [dict(row) for row in self.db.execute('''SELECT h.*, r.active AS reservation_active
            FROM hold_requests AS h LEFT JOIN stock_reservations AS r ON r.id=h.reservation_id
            ORDER BY h.created DESC, h.id DESC LIMIT ? OFFSET ?''', (limit, offset))]

    def hold_request_count(self):
        return self.db.execute('SELECT COUNT(*) FROM hold_requests').fetchone()[0]

    def stock_reservations(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM stock_reservations WHERE active=1 ORDER BY id DESC LIMIT 100')]

    def release_stock_reservation(self, reservation_id):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM stock_reservations WHERE id=? AND active=1', (reservation_id,)).fetchone()
            if not row:
                result = 'missing'
            elif row['generation'] != self.get('catalog_generation', 0):
                result = 'changed'
            else:
                cursor = self.db.execute('UPDATE products SET stock=stock+? WHERE id=? AND stock_known=1',
                                         (row['quantity'], row['product_id']))
                if not cursor.rowcount:
                    result = 'changed'
                else:
                    self.db.execute('UPDATE stock_reservations SET active=0 WHERE id=?', (reservation_id,))
                    self.set('catalog_version', self.get('catalog_version', 0) + 1)
                    result = 'released'
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def finish_hold(self, account_id: int, sender_id: int, quote_id: int,
                    products: list[Product], status: str, detail: str = '') -> int:
        if status not in {'sent', 'failed', 'uncertain'}:
            raise ValueError('Invalid final hold status')
        cursor = self.db.executemany('''UPDATE quote_holds SET status=?,detail=?
            WHERE account_id=? AND sender_id=? AND quote_id=? AND product_key=? AND status='pending' ''',
            [(status, detail[:120], account_id, sender_id, quote_id, self._hold_key(p)) for p in products])
        return cursor.rowcount

    @staticmethod
    def _hold_key(product: Product) -> str:
        return json.dumps([product_key(product), product.sku,
                           str(product.price) if product.price is not None else None],
                          ensure_ascii=False, separators=(',', ':'))

    def holds(self, account_id: int, sender_id: int, quote_id: int) -> list[dict]:
        return [dict(row) for row in self.db.execute('''SELECT * FROM quote_holds
            WHERE account_id=? AND sender_id=? AND quote_id=? ORDER BY created, rowid''',
            (account_id, sender_id, quote_id))]

    def _prune_quote_context(self, now: float):
        self.db.execute('DELETE FROM quotes WHERE created<?', (now - 86400,))
        self.db.execute('DELETE FROM quote_messages WHERE quote_id NOT IN (SELECT quote_id FROM quotes)')
        # Retain dedup metadata longer than quote context to protect replays.
        self.db.execute('DELETE FROM followups WHERE created<?', (now - 30*86400,))
        self.db.execute('DELETE FROM quote_holds WHERE created<?', (now - 30*86400,))

    def prune(self):
        now = time.time()
        self.db.execute("DELETE FROM deliveries WHERE created<? AND status IN ('sent','failed','cancelled')", (now - 30*86400,))
        # Keep message dedup metadata longer than quote context, so pruning an
        # expired quote cannot turn a replay into a new reply to a newer quote.
        self._prune_quote_context(now)

    def close(self):
        self.db.close()
