from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from .catalog import Product, normalize, product_key
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
            CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
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
        """)
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(products)')}
        for column, definition in [('stock_known', 'INTEGER NOT NULL DEFAULT 1'), ('country', "TEXT NOT NULL DEFAULT ''"),
                                   ('price_country', "TEXT NOT NULL DEFAULT ''"), ('min_qty', 'INTEGER NOT NULL DEFAULT 1'),
                                   ('warnings', "TEXT NOT NULL DEFAULT '[]'")]:
            if column not in columns:
                self.db.execute(f'ALTER TABLE products ADD COLUMN {column} {definition}')
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

    def products(self) -> list[Product]:
        return [Product(row['name'], Decimal(row['price']) if row['price'] else None,
                        row['stock'] if row['stock_known'] else None, row['sku'], row['country'],
                        row['price_country'], row['min_qty'], tuple(json.loads(row['warnings'])))
                for row in self.db.execute("SELECT * FROM products ORDER BY id")]

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

    def replace_products(self, products: list[Product]):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._write_products(products)
            self.set('price_source', None)
            self.set('price_ready', True)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def apply_price_snapshot(self, snapshot, source: str) -> dict:
        previous_source = self.get('price_source')
        if not snapshot.complete and previous_source != source and self.products():
            raise PriceFeedError('Для смены источника нужен полный прайс с итоговым количеством товаров')
        old = {product_key(p): p for p in self.products()} if previous_source == source else {}
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
            self._write_products(resulting.values())
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

    def toggle_chat(self, chat_id: int, title: str) -> bool:
        if chat_id in self.chats():
            self.db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
            return False
        self.db.execute("INSERT INTO chats VALUES (?,?)", (chat_id, title))
        return True

    def reserve(self, chat_id: int, message_id: int, sender_id: int, text: str,
                dedup_seconds: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        key = hashlib.sha256(normalize(text).encode()).hexdigest()
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
        return self.db.execute("SELECT COUNT(*) FROM deliveries WHERE status='sent' AND created>?", (since,)).fetchone()[0]

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

    def prune(self):
        self.db.execute("DELETE FROM deliveries WHERE created<? AND status IN ('sent','failed','cancelled')", (time.time() - 30*86400,))

    def close(self):
        self.db.close()
