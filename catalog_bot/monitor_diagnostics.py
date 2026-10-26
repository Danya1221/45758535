"""Read-only explanations for messages in the owner's selected groups."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from telethon.errors import RPCError

from .catalog import inspect_request, intent
from .engine import Incoming
from .pricing import price_products
from .storage import Store


REASONS = {
    'received': 'сообщение получено, обработка ещё не завершена',
    'sent': 'ответ отправлен',
    'own_message': 'сообщение отправлено подключённым аккаунтом; на свои сообщения бот не отвечает',
    'bot': 'сообщение от другого бота',
    'forwarded': 'пересланное сообщение',
    'empty': 'нет текста запроса',
    'too_long': 'текст длиннее 12 000 символов',
    'missing_sender': 'нет личного получателя: автор не определён либо пишет анонимно или от канала',
    'not_group': 'сообщение не из группы',
    'unselected': 'группа не выбрана для ответов',
    'paused': 'на момент обработки ответы были на паузе',
    'account_unavailable': 'аккаунт Telegram не был готов к получению сообщений',
    'price_unavailable': 'прайс ожидает успешного обновления',
    'before_enable': 'сообщение отправлено до включения ответов',
    'before_start': 'сообщение отправлено до запуска этого процесса; нового события для него нет',
    'old': 'сообщение не прошло проверку возраста',
    'no_match': 'в прайсе нет позиции, подходящей под все условия запроса',
    'bare_disabled': 'запрос без слова «куплю» пропущен настройкой',
    'not_request': 'не распознан однозначный запрос на покупку',
    'sell': 'распознано предложение о продаже',
    'rate_limited': 'достигнут лимит ответов в минуту',
    'rate_limit': 'достигнут лимит ответов в минуту',
    'duplicate': 'на этот запрос уже отвечали или начали отправку',
    'cancelled': 'во время подбора изменились прайс, наценка, выбор групп или состояние ответов',
    'sender_unavailable': 'не удалось получить сведения об отправителе',
    'failed': 'не удалось отправить ответ',
    'uncertain': 'результат отправки неизвестен; повторная отправка заблокирована',
    'pending': 'отправка начата, окончательный результат ещё не записан',
    'error': 'ошибка при обработке сообщения',
    'ignored': 'сообщение не прошло предварительные проверки',
    'not_observed': 'совпадение есть, но событие не зафиксировано; проверь получение новых сообщений',
}


def reason_label(result: str, detail: str = '') -> str:
    """Never put exception/request text into owner-visible diagnostics."""
    return REASONS.get(result, 'неизвестный результат обработки')


@dataclass(frozen=True)
class MessageDiagnostic:
    result: str
    observed_at: float | None = None
    current_match_details: str = ''


def _current_match(store: Store, text: str) -> MessageDiagnostic:
    allow_bare = store.get('allow_bare', True)
    products = price_products(store.products(), store.markup_policy())
    report = inspect_request(text, products, allow_bare)
    if report.products:
        return MessageDiagnostic('not_observed', current_match_details=f'Подходящих позиций сейчас: {len(report.products)}.')
    if not allow_bare and intent(text, True) == 'bare':
        return MessageDiagnostic('bare_disabled', current_match_details='Названия без «куплю» отключены. Добавь «куплю» к запросу.')
    if report.classification not in {'buy', 'bare', 'sell'}:
        return MessageDiagnostic('not_request', current_match_details=report.details[0][:220] if report.details else '')
    details = next((line for line in report.details if line.startswith('Пропущено:')), '')
    if not details:
        details = next((line for line in report.details if line.startswith('Отсеяно:')), '')
    return MessageDiagnostic('sell' if report.classification == 'sell' else 'no_match',
                             current_match_details=details[:220])


def diagnose_message(store: Store, message: Incoming, *, account_ready: bool = True,
                     now: float | None = None, max_age: int = 120,
                     monitor_started_at: float = 0) -> MessageDiagnostic:
    """Explain recorded outcomes, or evaluate a message without reserving/sending.

    An unobserved message is checked against the *current* configuration only.
    This does not claim that the monitor received or processed that message.
    """
    now = time.time() if now is None else now
    check = store.message_check(message.chat_id, message.message_id)
    delivery = store.delivery(message.chat_id, message.message_id)
    if delivery:
        return MessageDiagnostic(delivery['status'], check['observed'] if check else delivery['created'])
    if check:
        details = ''
        if check['result'] == 'no_match' and store.get('price_ready', True):
            details = _current_match(store, message.text).current_match_details
        return MessageDiagnostic(check['result'], check['observed'], details)
    if message.outgoing:
        return MessageDiagnostic('own_message')
    if message.is_bot:
        return MessageDiagnostic('bot')
    if message.forwarded:
        return MessageDiagnostic('forwarded')
    if not message.is_group:
        return MessageDiagnostic('not_group')
    if message.sender_id <= 0:
        return MessageDiagnostic('missing_sender')
    if not message.text.strip():
        return MessageDiagnostic('empty')
    if len(message.text) > 12000:
        return MessageDiagnostic('too_long')
    if message.chat_id not in store.chats():
        return MessageDiagnostic('unselected')
    if not account_ready:
        return MessageDiagnostic('account_unavailable')
    if not store.get('active', False):
        return MessageDiagnostic('paused')
    if message.timestamp < store.get('monitor_enabled_at', 0):
        return MessageDiagnostic('before_enable')
    if not store.get('price_ready', True):
        return MessageDiagnostic('price_unavailable')
    if not -30 <= now - message.timestamp <= max_age:
        return MessageDiagnostic('old')
    if message.timestamp < int(monitor_started_at):
        return MessageDiagnostic('before_start')
    return _current_match(store, message.text)


async def diagnose_chat(client, store: Store, chat_id: int, *, account_ready: bool = True,
                        now: float | None = None, max_age: int = 120,
                        monitor_started_at: float = 0, limit: int = 5) -> str:
    """Read at most five recent messages in an explicitly selected group."""
    chats = store.chats()
    if chat_id not in chats:
        return 'Эта группа не выбрана для ответов. Выбери её в /chats.'
    if not account_ready:
        return 'Аккаунт Telegram не подключён. Проверь /status и войди через /login.'
    try:
        messages = await asyncio.wait_for(client.get_messages(chat_id, limit=max(1, min(5, limit))), 20)
    except (RPCError, OSError, asyncio.TimeoutError, ValueError) as exc:
        return ('Не удалось прочитать последние сообщения выбранной группы. '
                f'Причина: {type(exc).__name__}. Проверь соединение и доступ аккаунта к группе.')
    now = time.time() if now is None else now
    lines = [f'🔧 Группа: {chats[chat_id][:150]}',
             'Сейчас ответы ' + ('включены.' if store.get('active', False) else 'на паузе.'),
             'Прайс ' + ('готов.' if store.get('price_ready', True) else 'ожидает обновления.'),
             'Проверка только читает сообщения и ничего не отправляет в группу.']
    for raw in list(messages)[:5]:
        text = getattr(raw, 'raw_text', '') or ''
        date = getattr(raw, 'date', None)
        if not text.strip() or date is None:
            continue
        message = Incoming(chat_id, raw.id, getattr(raw, 'sender_id', None) or 0, text,
                           date.timestamp(), outgoing=bool(getattr(raw, 'out', False)),
                           is_bot=bool(getattr(getattr(raw, 'sender', None), 'bot', False)),
                           is_group=True, forwarded=bool(getattr(raw, 'fwd_from', None)))
        diagnostic = diagnose_message(store, message, account_ready=account_ready, now=now,
                                      max_age=max_age, monitor_started_at=monitor_started_at)
        preview = ' '.join(text.split())[:100]
        lines += ['', f'Сообщение {raw.id}: «{preview}»']
        if diagnostic.observed_at is None:
            lines.append('Событие не зафиксировано. Проверка по текущим настройкам:')
        elif diagnostic.observed_at < monitor_started_at:
            lines.append('Зафиксированный результат до перезапуска:')
        else:
            lines.append('Зафиксированный результат:')
        lines.append(reason_label(diagnostic.result) + '.')
        if diagnostic.current_match_details:
            lines.append('По текущему прайсу: ' + diagnostic.current_match_details)
    if len(lines) == 4:
        lines += ['', 'В последних сообщениях нет текстовых запросов. Отправь новый запрос с другого аккаунта.']
    return '\n'.join(lines)[:3900]
