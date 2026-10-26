from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import os
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
from dotenv import load_dotenv
from telethon import TelegramClient, errors, events, types
from telethon.errors import FloodWaitError, RPCError

from .catalog import CatalogError, build_reply, inspect_request, parse_catalog
from .engine import Engine, Incoming, SendFailure
from .storage import Store
from .prices import FOOTER, PriceFeedError, parse_price_feed
from .price_sync import PriceSync, format_summary
from .pricing import CATEGORIES, MarkupPolicy, MarkupRule, category_for, format_policy, price_products, resolve_category
from .sessions import load_string_session, persist_session
from .account_login import AccountLogin, LoginError
from .login_qr import qr_png
from .price_schedule import PriceSchedule
from .group_directory import GroupDirectory, search_groups
from .monitor_diagnostics import diagnose_chat, reason_label
from .update_state import checkpoint_update_state, restore_channel_updates
from .message_chunks import split_messages

log = logging.getLogger(__name__)
RELEASE = '2026-10-26-all-matches'


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    data_dir: Path
    contact: str = ""
    max_per_minute: int = 10
    dedup_seconds: int = 120
    price_bot: str = ''
    price_request: str = ''
    price_button: str = ''
    price_interval: int = 600
    telegram_session: str = field(default='', repr=False)

    @classmethod
    def from_env(cls, *, login_only: bool = False):
        load_dotenv()
        required = ['API_ID', 'API_HASH'] + ([] if login_only else ['BOT_TOKEN', 'OWNER_ID'])
        missing = [key for key in required if not os.environ.get(key)]
        if missing:
            raise ValueError('Заполни переменные окружения (локально — .env): ' + ', '.join(missing))
        try:
            result = cls(int(os.environ['API_ID']), os.environ['API_HASH'], os.getenv('BOT_TOKEN', ''),
                         int(os.getenv('OWNER_ID') or 0),
                         Path(os.getenv('DATA_DIR') or os.getenv('RAILWAY_VOLUME_MOUNT_PATH') or 'data').resolve(),
                         os.getenv('CONTACT', '')[:300], int(os.getenv('MAX_REPLIES_PER_MINUTE', '10')),
                         int(os.getenv('DEDUP_SECONDS', '120')), os.getenv('PRICE_BOT_USERNAME', '').strip(),
                         os.getenv('PRICE_REQUEST_TEXT', '').strip(), os.getenv('PRICE_BUTTON_TEXT', '').strip(),
                         int(os.getenv('PRICE_REFRESH_SECONDS', '600')), os.getenv('TELEGRAM_SESSION', '').strip())
        except ValueError as exc:
            raise ValueError('API_ID, OWNER_ID и лимиты должны быть целыми числами') from exc
        if result.api_id <= 0 or (not login_only and result.owner_id <= 0):
            raise ValueError('API_ID и OWNER_ID должны быть больше нуля')
        if not 1 <= result.max_per_minute <= 30 or not 0 <= result.dedup_seconds <= 86400:
            raise ValueError('Лимит ответов: 1–30 в минуту; DEDUP_SECONDS: 0–86400')
        if not 60 <= result.price_interval <= 86400:
            raise ValueError('PRICE_REFRESH_SECONDS: от 60 до 86400 секунд')
        if result.price_bot and bool(result.price_request) == bool(result.price_button):
            raise ValueError('Для источника прайса укажи одно из PRICE_REQUEST_TEXT или PRICE_BUTTON_TEXT')
        if not login_only:
            if os.getenv('RAILWAY_PROJECT_ID') or os.getenv('RAILWAY_ENVIRONMENT_ID'):
                mount = os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
                if not mount:
                    raise ValueError('На Railway подключи Volume с путём /data и задай DATA_DIR=/data. См. RAILWAY.md.')
                if not result.data_dir.is_relative_to(Path(mount).resolve()):
                    raise ValueError('DATA_DIR должен находиться внутри Railway Volume. Для /data задай DATA_DIR=/data.')
            if result.telegram_session:
                load_string_session(result.telegram_session)
        result.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return result


class Inputs(StatesGroup):
    catalog = State()
    test = State()
    intro = State()
    price = State()
    login_phone = State()
    login_code = State()
    login_password = State()
    login_qr = State()
    price_timer = State()
    group_search = State()


class OwnerOnly(BaseMiddleware):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        message = event.message if isinstance(event, CallbackQuery) else event
        chat = getattr(message, 'chat', None)
        if user and user.id == self.owner_id and chat and chat.type == 'private':
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer('Нет доступа', show_alert=True)


def keyboard(rows: list[list[tuple[str, str]]]):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
    ])


def menu():
    return keyboard([
        [('🔐 По номеру', 'login'), ('📷 Войти по QR', 'loginqr')],
        [('💰 Запросить прайс', 'price'), ('💬 Группы', 'chats_all')],
        [('⏱ Автообновление', 'price_timer'), ('📥 Загрузить вручную', 'uploadprice')],
        [('🔎 Проверить запрос', 'test'), ('📊 Статус', 'status')],
        [('💸 Наценка', 'markup'), ('🔧 Почему нет ответа', 'why')],
        [('▶️ Включить ответы', 'run'), ('⏸ Пауза', 'pause')],
    ])


CATALOG_PROMPT = (
    'Пришли список текстом или файлом .txt/.csv (до 1 МБ).\n'
    'Каждая строка — один товар:\n\n'
    'Название | Цена в рублях | Остаток | Артикул\n\n'
    'Цена и артикул необязательны. Если остаток пропущен — считаем, что товар есть; '
    '0 — не предлагать. Новый список целиком заменит старый.\n'
    'Пример файла: /sample. Отмена: /cancel.'
)


class Application:
    def __init__(self, config: Config, store: Store, client: TelegramClient, bot: Bot):
        self.config, self.store, self.client, self.bot = config, store, client, bot
        self.dp = Dispatcher()
        self.dp.message.outer_middleware(OwnerOnly(config.owner_id))
        self.dp.callback_query.outer_middleware(OwnerOnly(config.owner_id))
        self.account_available = asyncio.Event()
        self.account_changed = asyncio.Event()
        self.refresh_requested = asyncio.Event()
        self.auth = AccountLogin(client, config.data_dir, self.account_ready)
        self.qr_task: asyncio.Task | None = None
        self.chat_ui_lock = asyncio.Lock()
        self.chat_origin = self.chat_panel = None
        self.chat_page = 0
        self.chat_query = ''
        self.monitor_started_at = time.time()
        self.updates_checked = False
        self.chat_signature = None
        self.group_directory = GroupDirectory(store, client, self.account_available, self.update_chats_panel)
        self.price_schedule = PriceSchedule(store, config.price_interval)
        self.engine = Engine(store, self.send_reply, max_per_minute=config.max_per_minute,
                             dedup_seconds=config.dedup_seconds, contact=config.contact)
        self.price_sync = PriceSync(store, client, config.price_bot, self.notify, request_text=config.price_request,
                                    button_text=config.price_button, interval=self.price_schedule.interval) if config.price_bot else None
        if self.price_sync:
            store.set('price_ready', False)
        self.register_handlers()

    async def account_ready(self, user):
        self.updates_checked = False
        await restore_channel_updates(self.client, int(user.id))
        await checkpoint_update_state(self.client)
        if self.group_directory.account_id != int(user.id):
            self.chat_query, self.chat_page = '', 0
        self.group_directory.bind(int(user.id))
        self.store.set('telegram_account_id', int(user.id))
        self.store.set('telegram_account_name', getattr(user, 'username', None) or getattr(user, 'first_name', None) or str(user.id))
        if self.price_sync:
            self.price_sync.peer = None
            self.store.set('price_ready', False)
        self.account_available.set()
        self.account_changed.set()
        self.price_schedule.mark_due()
        self.refresh_requested.set()

    def account_unavailable(self, *, authorization_lost=False):
        self.updates_checked = False
        self.account_available.clear()
        self.refresh_requested.set()
        if self.price_sync:
            self.store.set('price_ready', False)
        if authorization_lost:
            self.group_directory.cancel()
            self.auth.ready, self.auth.user = False, None
            self.store.set('active', False)

    async def account_loop(self):
        while True:
            self.account_changed.clear()
            was_ready = self.auth.ready
            retry_delay = 30
            try:
                ready = await self.auth.restore()
                if ready:
                    if not self.account_available.is_set():
                        if self.price_sync:
                            self.store.set('price_ready', False)
                        self.account_available.set()
                        self.price_schedule.mark_due()
                        self.refresh_requested.set()
                    try:
                        await asyncio.wait_for(asyncio.shield(self.client.disconnected), 30)
                    except asyncio.TimeoutError:
                        continue
                    self.account_unavailable()
                else:
                    self.account_unavailable(authorization_lost=True)
                    if was_ready:
                        await self.notify('Авторизация Telegram потеряна. Ответы на паузе. Для входа: /login')
            except (RPCError, OSError, asyncio.TimeoutError, ValueError, LoginError) as exc:
                # Never include raw authentication errors or credential values.
                log.warning('Account connection: %s', type(exc).__name__)
                lost = isinstance(exc, RPCError) and getattr(exc, 'code', None) == 401
                self.account_unavailable(authorization_lost=lost)
                if isinstance(exc, FloodWaitError):
                    self.auth.retry_until = time.monotonic() + exc.seconds
                    retry_delay = max(retry_delay, exc.seconds)
            try:
                await asyncio.wait_for(self.account_changed.wait(), retry_delay)
            except asyncio.TimeoutError:
                pass

    async def price_loop(self):
        while True:
            await self.account_available.wait()
            self.refresh_requested.clear()
            delay = self.price_schedule.remaining()
            if delay is not None:
                delay = max(delay, self.price_sync.retry_after - time.monotonic())
                if delay <= 0:
                    if await self.refresh_source_price() is not None:
                        continue
                    delay = None  # A manual request will wake us on completion.
            try:
                await asyncio.wait_for(self.refresh_requested.wait(), delay)
            except asyncio.TimeoutError:
                pass

    def price_source_busy(self):
        lock = getattr(self.price_sync, 'lock', None)
        return lock is not None and lock.locked()

    async def refresh_source_price(self):
        if self.price_source_busy():
            return None
        try:
            return await self.price_sync.refresh()
        finally:
            self.price_schedule.completed()
            self.refresh_requested.set()

    async def request_price(self, message: Message, state: FSMContext):
        if not self.price_sync:
            await message.answer('Источник прайса не настроен. Добавь PRICE_BOT_USERNAME и команду PRICE_REQUEST_TEXT в Railway.\n'
                                 'Загрузить прайс вручную: /uploadprice.', reply_markup=menu())
            return
        if not await self.require_account(message):
            return
        await state.clear()
        if self.price_source_busy():
            await message.answer('Прайс уже запрашивается. Дождись результата.', reply_markup=menu())
            return
        await message.answer(f'Запрашиваю прайс у {self.config.price_bot}…')
        if await self.refresh_source_price():
            await message.answer('Прайс обновлён. Изменения цен: /changes.', reply_markup=menu())

    def price_timer_status(self):
        if not self.price_sync:
            return 'Автообновление: источник не настроен.'
        if not self.price_schedule.enabled:
            return 'Автообновление: выключено. Запросить сейчас: /price.'
        interval = self.price_schedule.interval
        period = f'{interval // 60} мин' if interval % 60 == 0 else f'{interval} сек'
        text = f'Автообновление: каждые {period}.'
        if not self.account_available.is_set():
            return text + '\nСледующий запрос: после подключения аккаунта.'
        if self.price_source_busy():
            return text + '\nСейчас загружаю прайс.'
        remaining = max(self.price_schedule.remaining() or 0, self.price_sync.retry_after - time.monotonic())
        seconds = max(0, int(remaining + .999))
        stamp = datetime.fromtimestamp(self.price_schedule.clock() + remaining, ZoneInfo('Europe/Moscow')).strftime('%H:%M:%S МСК')
        return text + f'\nСледующий запрос: {stamp}, через {seconds // 60:02d}:{seconds % 60:02d}.'

    async def show_price_timer(self, message: Message):
        stamp = self.store.get('price_updated_at')
        last = datetime.fromtimestamp(stamp, ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M:%S МСК') if stamp else 'ещё не было'
        await message.answer('⏱ Обновление прайса\n'
                             f'Источник: {self.config.price_bot or "не настроен"}\n'
                             f'{self.price_timer_status()}\nПоследний сохранённый прайс: {last}\n\n'
                             'Выбери интервал или задай свой. После завершения запроса отсчёт начинается заново.',
                             reply_markup=keyboard([
                                 [('5 мин', 'timer:5'), ('10 мин', 'timer:10'), ('30 мин', 'timer:30'), ('1 час', 'timer:60')],
                                 [('Свой интервал', 'timer:custom'),
                                  ('⏸ Выключить' if self.price_schedule.enabled else '▶️ Включить',
                                   'timer:off' if self.price_schedule.enabled else 'timer:on')],
                                 [('💰 Запросить сейчас', 'price'), ('🔄 Таймер', 'price_timer')],
                                 [('Меню', 'home')]]))

    async def set_price_timer(self, message: Message, state: FSMContext, value: str):
        value = value.strip().lower()
        if value in {'off', 'выкл', '0'}:
            seconds = None
        elif value == 'on':
            seconds = self.price_schedule.interval
        elif value.isascii() and value.isdecimal() and 1 <= len(value) <= 4 and 1 <= int(value) <= 1440:
            seconds = int(value) * 60
        else:
            await message.answer('Введи интервал от 1 до 1440 минут. Например: /timer 10. Выключить: /timer off.')
            return
        self.price_schedule.configure(seconds)
        if seconds is not None and not self.store.get('price_ready', True):
            self.price_schedule.mark_due()
        if self.price_sync:
            self.price_sync.interval = self.price_schedule.interval
        self.refresh_requested.set()
        await state.clear()
        await self.show_price_timer(message)

    async def upload_price(self, message: Message, state: FSMContext):
        await state.set_state(Inputs.price)
        await state.update_data(price_parts=[])
        await message.answer('Пришли прайс с блоками «Название» и «От 1 шт — цена» текстом, частями или файлом .txt.\n'
                             'Дождись строки «Всего найдено N товаров». Без итоговой строки можно завершить кнопкой: '
                             'обновятся только присланные позиции. Отмена: /cancel.',
                             reply_markup=keyboard([[('Завершить загрузку части прайса', 'price_done')]]))

    async def require_account(self, message: Message) -> bool:
        if not self.auth.ready:
            await message.answer('Сначала подключи аккаунт Telegram: /login')
            return False
        if not self.account_available.is_set() or not self.client.is_connected():
            await message.answer('Соединение с аккаунтом восстанавливается. Проверь /status немного позже.')
            return False
        return True

    def cancel_login(self):
        self.auth.cancel()
        task, self.qr_task = self.qr_task, None
        if task and not task.done():
            task.cancel()
        return task

    async def begin_login(self, message: Message, state: FSMContext):
        self.cancel_login()
        await state.clear()
        try:
            phase = await self.auth.begin()
        except LoginError as exc:
            await message.answer(str(exc))
            return
        if phase == 'ready':
            await message.answer(f'✅ Аккаунт уже подключён. Telegram ID: {self.auth.user.id}.\nДальше: /markup и /chats.', reply_markup=menu())
            return
        self.store.set('active', False)
        self.account_available.clear()
        await state.set_state(Inputs.login_phone)
        await message.answer('Пришли номер аккаунта с кодом страны, например +79991234567.\n'
                             'Если код не приходит: /loginqr. Отмена: /cancel.')

    async def begin_login_qr(self, message: Message, state: FSMContext):
        self.cancel_login()
        await state.clear()
        try:
            generation = await self.auth.begin_qr()
        except LoginError as exc:
            await message.answer(str(exc))
            return
        if generation is None:
            await message.answer(f'✅ Аккаунт уже подключён. Telegram ID: {self.auth.user.id}.', reply_markup=menu())
            return
        self.store.set('active', False)
        self.account_available.clear()
        await state.set_state(Inputs.login_qr)
        self.qr_task = asyncio.create_task(self.qr_login_worker(message, state, generation))

    async def qr_login_worker(self, message: Message, state: FSMContext, generation: int):
        photo = None
        caption = ('На телефоне открой Telegram → Настройки → Устройства → Подключить устройство.\n'
                   'Отсканируй этот QR с экрана компьютера и подтверди вход.\n'
                   'QR обновляется автоматически до 5 минут. Отмена: /cancel.')
        try:
            while True:
                png = await asyncio.to_thread(qr_png, self.auth.qr_url(generation))
                self.auth._check_generation(generation)
                image = BufferedInputFile(png, filename='login_qr.png')
                if photo is None:
                    photo = await message.answer_photo(image, caption=caption, protect_content=True)
                else:
                    await photo.edit_media(InputMediaPhoto(media=image, caption=caption))
                self.auth._check_generation(generation)
                result = await self.auth.wait_qr(generation)
                if result != 'refresh':
                    await self.login_result(message, state, result)
                    return
        except LoginError as exc:
            if self.qr_task is asyncio.current_task():
                self.auth.cancel()
                await state.clear()
                await message.answer(str(exc))
        except Exception:
            # Never log the exception/request: they can contain the QR token.
            log.warning('QR login display failed; diagnostic=LOGIN/display_qr')
            if self.qr_task is asyncio.current_task():
                self.auth.cancel()
                await state.clear()
                try:
                    await message.answer('Не удалось показать QR или завершить вход. Повтори /loginqr.\n'
                                         'Код диагностики: LOGIN/display_qr')
                except TelegramAPIError:
                    pass
        finally:
            if photo is not None:
                try:
                    await photo.delete()
                except TelegramAPIError:
                    pass
            if self.qr_task is asyncio.current_task():
                self.qr_task = None

    async def login_input(self, message: Message, state: FSMContext, phase: str):
        value = message.text or ''
        if phase in {'code', 'password'}:
            try:
                await message.delete()
            except TelegramAPIError:
                pass
        try:
            result = await getattr(self.auth, {'phone': 'phone_number', 'code': 'code', 'password': 'password'}[phase])(value)
        except LoginError as exc:
            if exc.restart:
                await state.clear()
            await message.answer(str(exc), reply_markup=keyboard([[('📷 Войти по QR', 'loginqr')]]) if exc.restart else None)
            return

        await self.login_result(message, state, result)

    async def login_result(self, message: Message, state: FSMContext, result: str):
        if result == 'code':
            await state.set_state(Inputs.login_code)
            await message.answer('Telegram отправил код входа. Введи его цифры через пробелы, например 1 2 3 4 5.\n'
                                 'Не пересылай сообщение с кодом. Отмена: /cancel.')
        elif result == 'password':
            await state.set_state(Inputs.login_password)
            await message.answer('Включена двухэтапная проверка. Пришли пароль 2FA. Отмена: /cancel.')
        else:
            await state.clear()
            await message.answer(f'✅ Аккаунт подключён. Telegram ID: {self.auth.user.id}. Авторизация сохранена.\n'
                                 'Ответы пока выключены. Настрой /markup, выбери /chats и проверь /test. Включить: /run.', reply_markup=menu())

    async def send_reply(self, recipient_id: int, source_message_id: int, text: str):
        # The source message ID belongs to the group. It must never be used
        # as reply_to in a different dialog or as a fallback destination.
        if recipient_id <= 0:
            raise SendFailure('recipient_not_user', pause=False)
        try:
            recipient = await asyncio.wait_for(self.client.get_input_entity(types.PeerUser(recipient_id)), 10)
        except (FloodWaitError, errors.FloodPremiumWaitError) as exc:
            raise SendFailure(f'flood_wait_{exc.seconds}s') from exc
        except ValueError as exc:
            raise SendFailure('recipient_unavailable', pause=False) from exc
        except RPCError as exc:
            raise SendFailure(type(exc).__name__) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise SendFailure('recipient_lookup_' + type(exc).__name__, pause=False) from exc
        if not isinstance(recipient, types.InputPeerUser) or recipient.user_id != recipient_id:
            raise SendFailure('recipient_not_user', pause=False)
        parts = split_messages(text)
        sent_parts = 0

        def failure(reason, *, uncertain=False, pause=True):
            prefix = f'partial_{sent_parts}_of_{len(parts)}_parts;' if sent_parts else ''
            return SendFailure(prefix + reason, uncertain=uncertain, pause=pause)

        try:
            for part in parts:
                if sent_parts:
                    await asyncio.sleep(1)
                await asyncio.wait_for(self.client.send_message(recipient, part, parse_mode=None,
                                                                link_preview=False), timeout=30)
                sent_parts += 1
        except (FloodWaitError, errors.FloodPremiumWaitError) as exc:
            raise failure(f'flood_wait_{exc.seconds}s') from exc
        except (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError,
                errors.UserBlockedError, errors.YouBlockedUserError,
                errors.InputUserDeactivatedError, errors.PremiumAccountRequiredError) as exc:
            raise failure(type(exc).__name__, pause=False) from exc
        except RPCError as exc:
            raise failure(type(exc).__name__) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise failure(type(exc).__name__, uncertain=True) from exc

    async def notify(self, text: str):
        try:
            await self.bot.send_message(self.config.owner_id, text[:4000], parse_mode=None)
        except TelegramAPIError as exc:
            log.warning('Owner notification failed: %s', type(exc).__name__)

    async def monitor(self, event):
        if event.chat_id not in self.store.chats():
            return
        chat_id, message_id = event.chat_id, event.id
        def record(result, detail=''):
            self.store.record_check(chat_id, message_id, result, detail)
        try:
            record('received')
            if not self.auth.ready or not self.account_available.is_set():
                record('account_unavailable')
                return
            if event.out:
                record('own_message')
                return
            if not event.is_group:
                record('not_group')
                return
            if not self.store.get('active', False):
                record('paused')
                return
            if event.date.timestamp() < self.store.get('monitor_enabled_at', 0):
                record('before_enable')
                return
            if event.message.fwd_from:
                record('forwarded')
                return
            if not event.sender_id or event.sender_id <= 0:
                record('missing_sender')
                return
            if not (event.raw_text or '').strip():
                record('empty')
                return
            if len(event.raw_text) > 12000:
                record('too_long')
                return
            try:
                sender = await asyncio.wait_for(event.get_sender(), 10)
            except (RPCError, OSError, asyncio.TimeoutError) as exc:
                # A sender lookup failure must not silently disable all groups.
                record('sender_unavailable', type(exc).__name__)
                return
            if sender is None:
                record('sender_unavailable')
                return
            message = Incoming(event.chat_id, event.id, event.sender_id or 0, event.raw_text or '',
                               event.date.timestamp(), outgoing=event.out, is_bot=bool(getattr(sender, 'bot', False)),
                               is_group=event.is_group, forwarded=bool(event.message.fwd_from))
            result = await self.engine.process(message)
            if result == 'ignored':
                result = ('bot' if message.is_bot else 'forwarded' if message.forwarded
                          else 'empty' if not message.text.strip() else 'ignored')
            record(result)
            if result == 'sent':
                title = self.store.chats().get(event.chat_id, str(event.chat_id))
                await self.notify(f'✅ Цена отправлена автору в личку\nПолучатель: {event.sender_id}\n'
                                  f'Запрос из группы: {title}\nСообщение: {event.id}')
            elif result in {'failed', 'uncertain'}:
                delivery = self.store.delivery(chat_id, message_id)
                detail = delivery['detail'] if delivery else result
                record(result, detail)
                if not self.store.get('active', False):
                    await self.notify(f'⏸ Ответы приостановлены. Результат: {result}; {detail}.\n'
                                      'Проверь /tail. Для возобновления: /run. Повторной отправки этого сообщения не будет.')
                else:
                    await self.notify(f'Не удалось отправить цену автору в личку. Причина: {detail}.\n'
                                      f'Запрос: чат {chat_id}, сообщение {message_id}.\n'
                                      'Ответы другим пользователям продолжаются. Подробности: /tail.')
        except Exception as exc:
            record('error', type(exc).__name__)
            log.error('Monitor error: %s', type(exc).__name__)
            self.store.set('active', False)
            await self.notify('⏸ Ответы приостановлены из-за ошибки обработки. Проверь журналы приложения.')

    def monitor_status(self):
        rows = self.store.latest_checks(selected_only=True)
        if not rows:
            return 'События выбранных групп пока не получены. Проверить: /why.'
        row = rows[0]
        stamp = datetime.fromtimestamp(row['observed'], ZoneInfo('Europe/Moscow')).strftime('%H:%M:%S МСК')
        title = self.store.chats().get(row['chat_id'], str(row['chat_id']))
        return f"Последнее событие: {stamp}, {title[:80]}, №{row['message_id']} — {reason_label(row['result'])}."

    async def show_why(self, message: Message, chat_id: int | None = None, page: int = 0):
        chats = self.store.chats()
        if not chats:
            await message.answer('Сначала выбери группы: /chats.')
            return
        if chat_id is not None and chat_id not in chats:
            await message.answer('Эта группа не выбрана. Проверь /chats.')
            return
        if chat_id is None and len(chats) == 1:
            chat_id = next(iter(chats))
        if chat_id is None:
            rows = list(chats.items())
            page = max(0, min(page, (len(rows) - 1) // 8))
            buttons = [[(title[:70], f'why_chat:{cid}')] for cid, title in rows[page * 8:(page + 1) * 8]]
            navigation = []
            if page:
                navigation.append(('←', f'why_page:{page - 1}'))
            if (page + 1) * 8 < len(rows):
                navigation.append(('→', f'why_page:{page + 1}'))
            if navigation:
                buttons.append(navigation)
            await message.answer('Выбери группу для проверки последних сообщений:', reply_markup=keyboard(buttons))
            return
        if not await self.require_account(message):
            return
        text = await diagnose_chat(self.client, self.store, chat_id,
                                   account_ready=self.auth.ready and self.account_available.is_set(),
                                   max_age=self.engine.max_age, monitor_started_at=self.monitor_started_at)
        await message.answer(text, parse_mode=None, reply_markup=keyboard([
            [('🔄 Проверить ещё раз', f'why_chat:{chat_id}')], [('Меню', 'home')]]))

    def status(self) -> str:
        products = self.store.products()
        return ('📊 Статус\n'
                f'Версия: {RELEASE}\n'
                f"Аккаунт: {self.store.get('telegram_account_name', 'подключён') if self.auth.ready else 'не подключён — /login'}\n"
                f"Соединение: {'есть' if self.account_available.is_set() and self.client.is_connected() else 'нет'}\n"
                f"Ответы: {'включены' if self.store.get('active', False) else 'пауза'}\n"
                f'Позиций: {len(products)}, подходят для предложения: {sum(p.eligible for p in products)}\n'
                f"Прайс: {'готов к использованию' if self.store.get('price_ready', True) else 'требует успешного обновления'}\n"
                f"Источник: {self.config.price_bot or 'ручная загрузка'}\n"
                f'{self.price_timer_status()}\n'
                f'Выбрано групп: {len(self.store.chats())}\n'
                + self.monitor_status() + '\n' +
                f"Названия без «куплю»: {'учитываются' if self.store.get('allow_bare', True) else 'пропускаются'}\n"
                'Куда отвечаем: в личку автору запроса, от подключённого аккаунта.\n'
                f'Лимит: {self.config.max_per_minute} ответов в минуту.\n'
                'Остаток не уменьшается при предложении товара.\n\n' +
                format_policy(self.store.markup_policy()))

    async def activate(self, message: Message):
        if not await self.require_account(message):
            return
        if not any(p.eligible for p in self.store.products()):
            await message.answer('Сначала запроси прайс /price, загрузи вручную /uploadprice или добавь список /catalog.')
        elif not self.store.get('price_ready', True):
            await message.answer('Дождись успешного обновления прайса: /refresh. Статус: /status')
        elif not self.store.chats():
            await message.answer('Сначала выбери группы: /chats')
        elif not self.client.is_connected():
            await message.answer('Нет соединения с аккаунтом Telegram. Ответы не включены.')
        else:
            if not self.updates_checked:
                try:
                    await asyncio.wait_for(self.client.set_receive_updates(True), 20)
                    self.updates_checked = True
                except (RPCError, OSError, asyncio.TimeoutError) as exc:
                    self.store.set('active', False)
                    await message.answer('Не удалось включить получение сообщений Telegram. '
                                         f'Причина: {type(exc).__name__}. Повтори /run позже.')
                    return
            if not self.store.get('active', False):
                # Telegram message dates have whole-second precision.
                self.store.set('monitor_enabled_at', int(time.time()))
            self.store.set('active', True)
            await message.answer('▶️ Ответы включены. Обрабатываю новые запросы в выбранных группах. '
                                 'Цены отправляются в личку автору запроса. Проверка: /why. Остановить: /pause')

    async def show_chats(self, message: Message, page: int = 0, refresh: bool = False, query: str | None = None):
        if not await self.require_account(message):
            return
        async with self.chat_ui_lock:
            if query is not None:
                self.chat_query = query
            same_origin = self.chat_origin and self.chat_origin.message_id == message.message_id
            same_panel = self.chat_panel and self.chat_panel.message_id == message.message_id
            if not same_origin and not same_panel:
                self.chat_origin, self.chat_panel, self.chat_signature = message, None, None
            self.chat_page = page
            task = self.group_directory.ensure_started(refresh=refresh)
            await self.render_chats_panel()
        # Tiny/local lists can finish immediately; network scans keep running
        # behind the visible progress panel instead of blocking the command.
        if task and not self.group_directory.items:
            try:
                await asyncio.wait_for(asyncio.shield(task), .15)
            except asyncio.TimeoutError:
                pass

    async def update_chats_panel(self):
        async with self.chat_ui_lock:
            if self.chat_origin is not None:
                await self.render_chats_panel()

    async def set_group_search(self, message: Message, state: FSMContext, value: str):
        if not await self.require_account(message):
            return
        query = ' '.join(value.split())
        if not query or len(query) > 200:
            await message.answer('Введи название или часть названия группы, от 1 до 200 символов. Отмена: /cancel.')
            return
        await state.clear()
        await self.show_chats(message, query=query)

    async def render_chats_panel(self):
        directory = self.group_directory
        dialogs = search_groups(directory.items, self.chat_query)
        page = min(max(0, self.chat_page), max(0, (len(dialogs) - 1) // 8))
        self.chat_page = page
        selected = self.store.chats()
        rows = [[(f"{'✅' if cid in selected else '⬜'} {title[:44]}", f'toggle:{page}:{cid}')]
                for cid, title in dialogs[page*8:page*8+8]]
        nav = []
        if page:
            nav.append(('←', f'chats:{page-1}'))
        if (page+1)*8 < len(dialogs):
            nav.append(('→', f'chats:{page+1}'))
        if nav:
            rows.append(nav)
        rows.append([('🔎 Найти группу', 'chats_search'), ('Все группы', 'chats_all')])
        rows.append([('🔄 Обновить', 'refresh'), ('Меню', 'home')])
        heading = (f'🔎 Поиск групп: {self.chat_query}\nНайдено: {len(dialogs)} из {len(directory.items)}.'
                   if self.chat_query else f'💬 Группы: {len(dialogs)}.')
        text = (f'{heading} Выбрано: {len(selected)}. Страница {page+1}.\n'
                f"Аккаунт: {self.store.get('telegram_account_name', 'подключён')}\n"
                'Нажми на группу, чтобы включить или выключить её.\n'
                'Сообщения без «куплю»: /bare on или /bare off.')
        if self.chat_query and not dialogs:
            text += ('\n\nПока совпадений нет. Результат обновится по мере загрузки групп.'
                     if directory.loading else '\n\nСреди загруженных групп совпадений нет. Попробуй другую часть названия.')
        wait = max(0, math.ceil(directory.retry_at - directory.clock()))
        if directory.loading:
            text += ('\n\nПродолжу загрузку автоматически через ' + str(wait) + ' сек.'
                     if wait else '\n\nЗагружаю список групп… Он появится здесь автоматически.')
            if dialogs:
                text += '\nУже найденные группы можно выбирать сейчас.'
        elif directory.error:
            text += '\n\n' + directory.error
        elif directory.complete and not dialogs and not self.chat_query:
            text += '\nПодключённый аккаунт пока не состоит в группах.'
        markup = keyboard(rows)
        signature = (text, markup.model_dump_json())
        if self.chat_panel is not None and signature == self.chat_signature:
            return
        if self.chat_panel is not None:
            try:
                await self.chat_panel.edit_text(text, reply_markup=markup, parse_mode=None)
                self.chat_signature = signature
                return
            except TelegramBadRequest:
                self.chat_panel = None
        self.chat_panel = await self.chat_origin.answer(text, reply_markup=markup, parse_mode=None)
        self.chat_signature = signature

    async def show_markup(self, message: Message):
        categories = '\n'.join(f'{key} — {label}' for key, label in CATEGORIES.items())
        await message.answer(
            format_policy(self.store.markup_policy()) + '\n\n'
            'Команды (цифры для примера):\n'
            '/markup default fixed 1000 — общее правило +1 000 ₽\n'
            '/markup phones percent 2 — телефоны +2%\n'
            '/markup tablets fixed 1500 — планшеты +1 500 ₽\n'
            '/markup phones off — телефоны без наценки\n'
            '/markup phones clear — вернуть общее правило\n'
            '/markup default off — общее правило 0\n'
            '/markup reset — убрать все наценки\n\n'
            'Правило категории заменяет общее. Фиксированная сумма и процент не складываются. '
            'Цена округляется до копеек. Бюджет покупателя проверяется по итоговой цене.\n\n'
            'Категории:\n' + categories,
            parse_mode=None, reply_markup=menu())

    async def show_test(self, message: Message, text: str):
        products = price_products(self.store.products(), self.store.markup_policy())
        if not products:
            await message.answer('Сначала загрузи список: /catalog')
            return
        report = await asyncio.to_thread(inspect_request, text, products, self.store.get('allow_bare', True))
        classification, found = report.classification, report.products
        labels = {'buy': 'запрос на покупку', 'bare': 'название товара без «куплю»',
                  'sell': 'предложение продавца', 'uncertain': 'неопределённое сообщение'}
        text_out = f'Тип: {labels[classification]}\nСовпадений: {len(found)}\nЦена для покупателя с настроенной наценкой.\n'
        details = '\n'.join(report.details)
        if len(details) > 1600:
            details = details[:1550] + '\n…Проверь остальные товарные строки отдельными запросами.'
        text_out += details + '\n'
        if found:
            categories = ', '.join(CATEGORIES[key] for key in sorted({category_for(p) for p in found}))
            text_out += f'Категории для наценки: {categories}.\n'
            text_out += '\nТакой ответ будет отправлен при включённых ответах:\n\n' + build_reply(found, self.store.get('intro'), self.config.contact)
        else:
            text_out += ('\nОтвета не будет. Проверь модель, версию, память, цвет, SIM и остаток. '
                         'Характеристика из запроса должна быть указана у товара. Для артикула заполни столбец «Артикул».')
        for part in split_messages(text_out):
            await message.answer(part, parse_mode=None)

    async def import_catalog(self, message: Message, state: FSMContext, text: str):
        try:
            products = await asyncio.to_thread(parse_catalog, text)
        except CatalogError as exc:
            await message.answer(f'Список не изменён. {exc}\nИсправь и отправь заново, либо /cancel.')
            return
        self.store.replace_products(products)
        await state.clear()
        await message.answer(f'📦 Список заменён: {len(products)} позиций; доступны: {sum(p.eligible for p in products)}.\n'
                             'Проверка: /test нужен iPhone 17\nСкачать текущий список: /stock\n' +
                             ('Ответы продолжают работать.' if self.store.get('active', False) else 'Когда будешь готов: /run'), reply_markup=menu())

    async def import_price(self, message: Message, state: FSMContext, text: str):
        try:
            snapshot = await asyncio.to_thread(parse_price_feed, text)
            summary = self.store.apply_price_snapshot(snapshot, 'manual:price')
        except PriceFeedError as exc:
            await message.answer(f'Прайс не изменён: {exc}\nПришли недостающие части либо /cancel.')
            return
        await state.clear()
        await message.answer(format_summary(summary), reply_markup=menu())

    def register_handlers(self):
        dp = self.dp

        @dp.message(Inputs.login_password, F.text,
                    ~F.text.regexp(r'^/(?:cancel|login|loginqr|start|help)(?:@\w+)?(?:\s|$)'))
        async def login_password(message: Message, state: FSMContext):
            await self.login_input(message, state, 'password')

        @dp.message(Command('start', 'help'))
        async def start(message: Message, state: FSMContext):
            self.cancel_login()
            await state.clear()
            await message.answer('Бот находит запросы в выбранных группах и отправляет цены авторам в личку.\n\n'
                                 'Подключить аккаунт: /login — номер, код, затем пароль 2FA при необходимости.\n\n'
                                 '/loginqr — вход по QR через Telegram на телефоне, без запроса кода.\n\n'
                                 '1. /price — запросить прайс у поставщика; /uploadprice — загрузить вручную.\n'
                                 '/timer — автообновление; /catalog — свой список.\n'
                                 '2. /markup — настроить наценку.\n3. /chats — выбрать группы.\n'
                                 '4. /test — проверить запрос.\n5. /run — включить ответы.\n\n'
                                 '/pause — остановить; /status — статус; /stock — текущий список; '
                                 '/bare — запросы без «куплю»; /intro — начало ответа; /tail — последние отправки; '
                                 '/why — почему нет ответа в группе.', reply_markup=menu())

        @dp.message(Command('login'))
        async def login_account(message: Message, state: FSMContext):
            await self.begin_login(message, state)

        @dp.message(Command('loginqr'))
        async def login_account_qr(message: Message, state: FSMContext):
            await self.begin_login_qr(message, state)

        @dp.message(Command('price', 'refresh'))
        async def price(message: Message, state: FSMContext):
            await self.request_price(message, state)

        @dp.message(Command('uploadprice'))
        async def upload_price(message: Message, state: FSMContext):
            await self.upload_price(message, state)

        @dp.message(Command('timer'))
        async def timer(message: Message, state: FSMContext):
            value = (message.text or '').partition(' ')[2].strip()
            if value:
                await self.set_price_timer(message, state, value)
            else:
                await state.clear()
                await self.show_price_timer(message)

        @dp.message(Command('changes'))
        async def changes(message: Message):
            run, rows = self.store.latest_price_run()
            if not run:
                await message.answer('Прайс ещё не загружен: /price')
                return
            import json
            summary = json.loads(run['summary'])
            stamp = datetime.fromtimestamp(run['created'], ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
            lines = [stamp, format_summary(summary)]
            if not summary['baseline']:
                for row in rows:
                    old = row['old_price'] or '—'
                    new = row['new_price'] or '—'
                    labels = {'added': 'Добавлено', 'changed': 'Цена', 'removed': 'Исчезло из прайса'}
                    lines.append(f"{labels[row['kind']]}: {row['name']} {row['country']} | {old} → {new}")
            for product in [p for p in self.store.products() if p.warnings][:8]:
                lines.append(f'Спорная строка: {product.name} | у товара {product.country or "—"} | у цены {product.price_country or "—"}')
            await message.answer('\n'.join(lines)[:4000], parse_mode=None)

        @dp.message(Command('cancel'))
        async def cancel(message: Message, state: FSMContext):
            self.cancel_login()
            await state.clear()
            await message.answer('Ввод отменён.', reply_markup=menu())

        @dp.message(Command('catalog'))
        async def catalog(message: Message, state: FSMContext):
            await state.set_state(Inputs.catalog)
            await message.answer(CATALOG_PROMPT)

        @dp.message(Command('status'))
        async def status(message: Message):
            await message.answer(self.status(), reply_markup=menu())

        @dp.message(Command('why'))
        async def why(message: Message, state: FSMContext):
            await state.clear()
            await self.show_why(message)

        @dp.message(Command('chats'))
        async def chats(message: Message, state: FSMContext):
            value = (message.text or '').partition(' ')[2].strip()
            if value.lower() in {'refresh', 'обновить'}:
                await state.clear()
                await self.show_chats(message, refresh=True)
            elif value:
                await self.set_group_search(message, state, value)
            else:
                await state.clear()
                await self.show_chats(message, query='')

        @dp.message(Command('run'))
        async def run(message: Message):
            await self.activate(message)

        @dp.message(Command('pause'))
        async def pause(message: Message):
            self.store.set('active', False)
            await message.answer('⏸ Ответы выключены. Уже отправляемое сообщение может завершиться.', reply_markup=menu())

        @dp.message(Command('test'))
        async def test(message: Message, state: FSMContext):
            text = (message.text or '').partition(' ')[2].strip()
            if text:
                await state.clear()
                await self.show_test(message, text)
            else:
                await state.set_state(Inputs.test)
                await message.answer('Пришли пример запроса. Покажу результат без отправки покупателю. Отмена: /cancel.')

        @dp.message(Command('bare'))
        async def bare(message: Message):
            mode = (message.text or '').partition(' ')[2].strip().casefold()
            if mode in {'on', 'off'}:
                self.store.set('allow_bare', mode == 'on')
            await message.answer(f"Названия без «куплю»: {'учитываются' if self.store.get('allow_bare', True) else 'пропускаются'}.\n"
                                 '/bare on — учитывать; /bare off — требовать намерение купить.')

        @dp.message(Command('markup'))
        async def markup(message: Message, state: FSMContext):
            await state.clear()
            args = (message.text or '').partition(' ')[2].strip()
            if not args:
                await self.show_markup(message)
                return
            try:
                if args.casefold() == 'reset':
                    policy = MarkupPolicy()
                else:
                    policy = self.store.markup_policy()
                    parts = args.split(maxsplit=2)
                    if len(parts) < 2:
                        raise ValueError('Укажи категорию и правило: /markup phones percent 2')
                    category = resolve_category(parts[0])
                    mode = parts[1].casefold()
                    if mode in {'off', 'clear'} and len(parts) == 2:
                        policy = (policy.with_rule(category, MarkupRule.parse('fixed', '0')) if mode == 'off'
                                  else policy.without_rule(category))
                    elif len(parts) == 3:
                        policy = policy.with_rule(category, MarkupRule.parse(mode, parts[2]))
                    else:
                        raise ValueError('Формат: /markup phones fixed 1000 или /markup phones percent 2')
                self.store.set_markup_policy(policy)
            except ValueError as exc:
                await message.answer(f'Наценка не изменена. {exc}\nТекущие правила и справка: /markup', parse_mode=None)
                return
            await message.answer('✅ Наценка сохранена. Новые ответы и /test используют итоговую цену.\n\n' +
                                 format_policy(policy), parse_mode=None, reply_markup=menu())

        @dp.message(Command('intro'))
        async def intro(message: Message, state: FSMContext):
            await state.set_state(Inputs.intro)
            await message.answer('Пришли начало ответа, до 300 символов. Например: «Есть в наличии:». Отмена: /cancel.')

        @dp.message(Command('tail'))
        async def tail(message: Message):
            rows = self.store.recent()
            labels = {'sent': 'отправлено', 'failed': 'ошибка', 'uncertain': 'неизвестно, проверь личку с автором', 'pending': 'отправляется'}
            lines = [f"{labels.get(r['status'], r['status'])} | чат {r['chat_id']} | сообщение {r['message_id']} | {r['detail']}" for r in rows]
            await message.answer('\n'.join(lines) if lines else 'Отправок пока нет.')

        @dp.message(Command('stock'))
        async def stock(message: Message):
            products = self.store.products()
            if products and all(p.stock is None for p in products):
                lines = []
                for p in products:
                    lines.extend((f'• {p.name}{p.country}', f'{p.price_country} От {p.min_qty} шт - {p.price}', ''))
                lines.append(f'Всего найдено {len(products)} товаров')
                await message.answer_document(BufferedInputFile('\n'.join(lines).encode('utf-8-sig'), filename='my_price.txt'),
                                              caption='Исходные цены без наценки. Повторная загрузка — через /uploadprice. Остатки не указаны источником.')
                return
            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter='|', lineterminator='\n')
            writer.writerow(['Название', 'Цена', 'Остаток', 'Артикул'])
            for p in products:
                suffix = (' ' + (p.country or p.price_country)) if p.country or p.price_country else ''
                writer.writerow([p.name + suffix, str(p.price) if p.price is not None else '', p.stock if p.stock is not None else 'не указан', p.sku])
            await message.answer_document(BufferedInputFile(buffer.getvalue().encode('utf-8-sig'), filename='my_catalog.txt'),
                                          caption='Текущий список с исходными ценами без наценки.')

        @dp.message(Command('sample'))
        async def sample(message: Message):
            path = Path(__file__).resolve().parent.parent / 'examples' / 'catalog.txt'
            await message.answer_document(BufferedInputFile(path.read_bytes(), filename='catalog_example.txt'),
                                          caption='Учебный пример: цены вымышленные. Замени на свой список.')

        @dp.callback_query()
        async def callbacks(callback: CallbackQuery, state: FSMContext):
            if not isinstance(callback.message, Message):
                await callback.answer()
                return
            action = callback.data or ''
            await callback.answer()
            message = callback.message
            if action == 'login':
                await self.begin_login(message, state)
            elif action == 'loginqr':
                await self.begin_login_qr(message, state)
            elif action == 'catalog':
                await state.set_state(Inputs.catalog)
                await message.answer(CATALOG_PROMPT)
            elif action == 'price':
                await self.request_price(message, state)
            elif action == 'uploadprice':
                await self.upload_price(message, state)
            elif action == 'price_timer':
                await state.clear()
                await self.show_price_timer(message)
            elif action == 'timer:custom':
                await state.set_state(Inputs.price_timer)
                await message.answer('На сколько минут поставить автообновление? Введи число от 1 до 1440.\nВыключить: off. Отмена: /cancel.')
            elif action.startswith('timer:'):
                await self.set_price_timer(message, state, action.split(':', 1)[1])
            elif action == 'price_done':
                if await state.get_state() != Inputs.price.state:
                    await message.answer('Сначала начни загрузку: /uploadprice')
                    return
                parts = (await state.get_data()).get('price_parts', [])
                await self.import_price(message, state, '\n'.join(parts))
            elif action == 'test':
                await state.set_state(Inputs.test)
                await message.answer('Пришли пример запроса. Проверка ничего не отправляет покупателю. Отмена: /cancel.')
            elif action == 'markup':
                await state.clear()
                await self.show_markup(message)
            elif action == 'status':
                await message.answer(self.status(), reply_markup=menu())
            elif action == 'why':
                await state.clear()
                await self.show_why(message)
            elif action.startswith('why_chat:'):
                await self.show_why(message, chat_id=int(action.split(':', 1)[1]))
            elif action.startswith('why_page:'):
                await self.show_why(message, page=int(action.split(':', 1)[1]))
            elif action == 'home':
                self.cancel_login()
                await state.clear()
                await message.answer('Выбери действие:', reply_markup=menu())
            elif action == 'run':
                await self.activate(message)
            elif action == 'pause':
                self.store.set('active', False)
                await message.answer('⏸ Ответы выключены. Уже отправляемое сообщение может завершиться.', reply_markup=menu())
            elif action == 'chats_search':
                if await self.require_account(message):
                    await state.set_state(Inputs.group_search)
                    await message.answer('Введи название или часть названия группы из своего аккаунта.\n'
                                         'Например: закупки. Можно также: /chats закупки. Отмена: /cancel.')
            elif action == 'chats_all':
                await state.clear()
                await self.show_chats(message, query='')
            elif action == 'refresh':
                await self.show_chats(message, refresh=True)
            elif action.startswith('chats:'):
                await self.show_chats(message, int(action.split(':')[1]))
            elif action.startswith('toggle:'):
                _, page, chat_id = action.split(':')
                title = self.group_directory.items.get(int(chat_id))
                if title is None:
                    await message.answer('Список групп устарел. Обнови /chats.')
                    return
                self.store.toggle_chat(int(chat_id), title)
                try:
                    await message.edit_reply_markup(reply_markup=None)
                except TelegramAPIError:
                    pass
                await self.show_chats(message, int(page))

        @dp.message(Inputs.group_search, F.text, ~F.text.startswith('/'))
        async def group_search_input(message: Message, state: FSMContext):
            await self.set_group_search(message, state, message.text)

        @dp.message(Inputs.price_timer, F.text, ~F.text.startswith('/'))
        async def price_timer_input(message: Message, state: FSMContext):
            await self.set_price_timer(message, state, message.text)

        @dp.message(Inputs.login_phone, F.text, ~F.text.startswith('/'))
        async def login_phone(message: Message, state: FSMContext):
            await self.login_input(message, state, 'phone')

        @dp.message(Inputs.login_code, F.text, ~F.text.startswith('/'))
        async def login_code(message: Message, state: FSMContext):
            await self.login_input(message, state, 'code')

        @dp.message(Inputs.catalog, F.document)
        @dp.message(Inputs.price, F.document)
        async def catalog_document(message: Message, state: FSMContext):
            doc = message.document
            if not doc or (doc.file_size or 0) > 1_000_000 or Path(doc.file_name or '').suffix.casefold() not in {'.txt', '.csv'}:
                await message.answer('Нужен .txt или .csv до 1 МБ.')
                return
            buffer = io.BytesIO()
            try:
                await self.bot.download(doc, destination=buffer)
                data = buffer.getvalue()
                if len(data) > 1_000_000:
                    await message.answer('Файл больше 1 МБ. Список не изменён.')
                    return
                try:
                    text = data.decode('utf-8-sig')
                except UnicodeDecodeError:
                    text = data.decode('cp1251')
            except (UnicodeDecodeError, TelegramAPIError):
                await message.answer('Не удалось прочитать файл. Сохрани его в UTF-8 и отправь заново.')
                return
            if await state.get_state() == Inputs.price.state:
                previous = (await state.get_data()).get('price_parts', [])
                previous.append(text)
                await state.update_data(price_parts=previous)
                await self.import_price(message, state, '\n'.join(previous))
            else:
                await self.import_catalog(message, state, text)

        @dp.message(Inputs.price, F.text, ~F.text.startswith('/'))
        async def price_text(message: Message, state: FSMContext):
            parts = (await state.get_data()).get('price_parts', [])
            parts.append(message.text or '')
            joined = '\n'.join(parts)
            if len(joined.encode('utf-8')) > 1_000_000:
                await state.clear()
                await message.answer('Прайс больше 1 МБ. Начни заново: /uploadprice')
                return
            await state.update_data(price_parts=parts)
            if FOOTER.search(joined):
                await self.import_price(message, state, joined)
            else:
                await message.answer(f'Принято частей: {len(parts)}. Пришли остальные либо заверши загрузку части прайса.',
                                     reply_markup=keyboard([[('Завершить загрузку части прайса', 'price_done')]]))

        @dp.message(Inputs.catalog, F.text, ~F.text.startswith('/'))
        async def catalog_text(message: Message, state: FSMContext):
            await self.import_catalog(message, state, message.text or '')

        @dp.message(Inputs.test, F.text, ~F.text.startswith('/'))
        async def test_text(message: Message, state: FSMContext):
            await state.clear()
            await self.show_test(message, message.text or '')

        @dp.message(Inputs.intro, F.text, ~F.text.startswith('/'))
        async def intro_text(message: Message, state: FSMContext):
            text = (message.text or '').strip()
            if not 1 <= len(text) <= 300:
                await message.answer('Нужно от 1 до 300 символов.')
                return
            self.store.set('intro', text)
            await state.clear()
            await message.answer('Начало ответа сохранено.')

        @dp.message()
        async def fallback(message: Message):
            await message.answer('Загрузить список: /catalog. Проверить запрос: /test. Меню: /start.')


async def login(config: Config):
    client = TelegramClient(str(config.data_dir / 'account'), config.api_id, config.api_hash)
    try:
        await client.start()
        me = await client.get_me()
        print(f'Аккаунт подключён. Его Telegram ID: {me.id}. Укажи OWNER_ID владельца управления в .env.')
    finally:
        await client.disconnect()
    session = config.data_dir / 'account.session'
    if session.exists():
        session.chmod(0o600)


async def run(config: Config):
    store = Store(config.data_dir / 'catalog.sqlite3')
    client = bot = app = None
    tasks: list[asyncio.Task] = []
    try:
        session_path = config.data_dir / 'account.session'
        # Keep old StringSession deployments working, then use the persistent
        # account file on later boots. New deployments need no session variable.
        if config.telegram_session and not (session_path.exists() and store.get('telegram_account_id')):
            persist_session(load_string_session(config.telegram_session), config.data_dir)
        client = TelegramClient(str(config.data_dir / 'account'), config.api_id, config.api_hash,
                                catch_up=False, flood_sleep_threshold=0, request_retries=0)
        if session_path.exists():
            session_path.chmod(0o600)
        bot = Bot(config.bot_token)
        app = Application(config, store, client, bot)
        # Own messages are observed for diagnostics, but monitor/Engine never
        # answer them. This makes self-tests distinguishable from lost updates.
        client.add_event_handler(app.monitor, events.NewMessage())
        if app.price_sync:
            client.add_event_handler(app.price_sync.receive, events.NewMessage(incoming=True))
            client.add_event_handler(app.price_sync.receive, events.MessageEdited(incoming=True))
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
            ('start', 'Меню'), ('login', 'Войти по номеру'), ('loginqr', 'Войти по QR'),
            ('price', 'Запросить прайс у поставщика'), ('uploadprice', 'Загрузить прайс вручную'),
            ('timer', 'Автообновление прайса'), ('changes', 'Изменения цен'),
            ('refresh', 'Обновить прайс из бота'), ('catalog', 'Загрузить список'), ('chats', 'Выбрать группы'),
            ('markup', 'Наценка по категориям'),
            ('test', 'Проверить запрос'), ('run', 'Включить ответы'), ('pause', 'Пауза'),
            ('why', 'Почему нет ответа в группе'),
            ('status', 'Статус'), ('stock', 'Скачать текущий список'), ('tail', 'Последние отправки')]])
        store.prune()
        await app.notify('Бот запущен. Проверяю сохранённый вход. Статус: /status. Войти при необходимости: /login.')
        tasks = [asyncio.create_task(app.dp.start_polling(bot, close_bot_session=False)),
                 asyncio.create_task(app.account_loop())]
        if app.price_sync:
            tasks.append(asyncio.create_task(app.price_loop()))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        if app is not None:
            group_task = app.group_directory.cancel()
            if group_task is not None:
                tasks.append(group_task)
            qr_task = app.cancel_login()
            if qr_task is not None:
                tasks.append(qr_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            await client.disconnect()
        if bot is not None:
            await bot.session.close()
        store.close()
