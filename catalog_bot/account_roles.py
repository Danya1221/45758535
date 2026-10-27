"""Separate the existing supplier login from a fresh reply-account login."""
from __future__ import annotations

from pathlib import Path

from .storage import Store


def prepare_account_roles(store: Store, data_dir: str | Path) -> tuple[Path, Path]:
    """Return (reply directory, price directory) without touching session files."""
    price_dir = Path(data_dir)
    reply_dir = price_dir / 'replies'
    for directory in (price_dir, reply_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)

    store.db.execute('BEGIN IMMEDIATE')
    try:
        if not store.get('role_split_initialized', False):
            account_id = store.get('telegram_account_id')
            account_name = store.get('telegram_account_name')
            if account_id is not None:
                store.set('price_account_id', account_id)
            if account_name is not None:
                store.set('price_account_name', account_name)
            store.set('legacy_selected_chats', list(store.chats().items()))
            store.db.execute('DELETE FROM chats')
            store.set('group_directory', {})
            store.set('telegram_account_id', None)
            store.set('telegram_account_name', None)
            store.set('active', False)
            store.set('role_split_initialized', True)
        store.db.execute('COMMIT')
    except BaseException:
        store.db.execute('ROLLBACK')
        raise
    return reply_dir, price_dir
