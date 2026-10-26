from __future__ import annotations

import os
from pathlib import Path

from telethon.sessions import SQLiteSession, StringSession


def persist_session(session, data_dir: Path) -> Path:
    """Persist an authorized key without printing or returning its contents."""
    target = data_dir / 'account.session'
    if not session.auth_key:
        raise ValueError('Нет ключа авторизации Telegram')
    if isinstance(session, SQLiteSession) and Path(session.filename).resolve() == target.resolve():
        session.save()
    else:
        saved = SQLiteSession(str(data_dir / 'account'))
        try:
            saved.set_dc(session.dc_id, session.server_address, session.port)
            saved.auth_key = session.auth_key
            saved.save()
        finally:
            saved.close()
    target.chmod(0o600)
    return target


def load_string_session(value: str) -> StringSession:
    """Validate locally without exposing the account key in an error."""
    try:
        session = StringSession(value)
        if not session.auth_key or not session.server_address or not session.port:
            raise ValueError('Empty session')
        return session
    except Exception:
        raise ValueError('Неверный TELEGRAM_SESSION. Удали старую переменную из настроек и войди через /login в управляющем боте.') from None


def export_session(data_dir: Path) -> Path:
    """Write a private Railway variable file; never print the session itself."""
    if not (data_dir / 'account.session').is_file():
        raise ValueError('Сначала выполни локально: python -m catalog_bot login')
    session = SQLiteSession(str(data_dir / 'account'))
    try:
        if not session.auth_key:
            raise ValueError('В локальной сессии нет авторизации. Сначала выполни login.')
        value = StringSession.save(session)
        load_string_session(value)
    finally:
        session.close()
    target = data_dir / 'railway-session.env'
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        if hasattr(os, 'fchmod'):
            os.fchmod(stream.fileno(), 0o600)
        stream.write('TELEGRAM_SESSION=' + value + '\n')
    return target
