from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError

from .catalog import CatalogError, build_reply, find_products, parse_catalog
from .engine import Engine, Incoming, SendFailure
from .storage import Store
from .prices import FOOTER, PriceFeedError, parse_price_feed
from .price_sync import PriceSync, format_summary
from .sessions import load_string_session

log = logging.getLogger(__name__)


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
        [('💰 Загрузить прайс', 'price'), ('💬 Группы', 'chats:0')],
        [('🔎 Проверить запрос', 'test'), ('📊 Статус', 'status')],
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
        self.dialogs: list[tuple[int, str]] = []
        self.dialogs_loaded = False
        self.engine = Engine(store, self.send_reply, max_per_minute=config.max_per_minute,
                             dedup_seconds=config.dedup_seconds, contact=config.contact)
        self.price_sync = PriceSync(store, client, config.price_bot, self.notify, request_text=config.price_request,
                                    button_text=config.price_button, interval=config.price_interval) if config.price_bot else None
        if self.price_sync:
            store.set('price_ready', False)
        self.register_handlers()

    async def send_reply(self, chat_id: int, message_id: int, text: str):
        try:
            await asyncio.wait_for(self.client.send_message(chat_id, text, reply_to=message_id,
                                                            parse_mode=None, link_preview=False), timeout=30)
        except FloodWaitError as exc:
            raise SendFailure(f'flood_wait_{exc.seconds}s') from exc
        except RPCError as exc:
            raise SendFailure(type(exc).__name__) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise SendFailure(type(exc).__name__, uncertain=True) from exc

    async def notify(self, text: str):
        try:
            await self.bot.send_message(self.config.owner_id, text[:4000], parse_mode=None)
        except TelegramAPIError as exc:
            log.warning('Owner notification failed: %s', type(exc).__name__)

    async def monitor(self, event):
        if event.out or not event.is_group or event.chat_id not in self.store.chats() or not self.store.get('active', False):
            return
        try:
            sender = await event.get_sender()
            message = Incoming(event.chat_id, event.id, event.sender_id or 0, event.raw_text or '',
                               event.date.timestamp(), outgoing=event.out, is_bot=bool(getattr(sender, 'bot', False)),
                               is_group=event.is_group, forwarded=bool(event.message.fwd_from))
            result = await self.engine.process(message)
            if result == 'sent':
                title = self.store.chats().get(event.chat_id, str(event.chat_id))
                await self.notify(f'✅ Ответ отправлен\nГруппа: {title}\nСообщение: {event.id}')
            elif result in {'failed', 'uncertain'}:
                detail = self.store.recent()[0]['detail']
                await self.notify(f'⏸ Ответы приостановлены. Результат: {result}; {detail}.\n'
                                  'Проверь /tail. Для возобновления: /run. Повторной отправки этого сообщения не будет.')
        except Exception as exc:
            log.error('Monitor error: %s', type(exc).__name__)
            self.store.set('active', False)
            await self.notify('⏸ Ответы приостановлены из-за ошибки обработки. Проверь журналы приложения.')

    def status(self) -> str:
        products = self.store.products()
        return ('📊 Статус\n'
                f"Ответы: {'включены' if self.store.get('active', False) else 'пауза'}\n"
                f'Позиций: {len(products)}, подходят для предложения: {sum(p.eligible for p in products)}\n'
                f"Прайс: {'готов к использованию' if self.store.get('price_ready', True) else 'требует успешного обновления'}\n"
                f"Источник: {self.config.price_bot or 'ручная загрузка'}\n"
                f'Выбрано групп: {len(self.store.chats())}\n'
                f"Названия без «куплю»: {'учитываются' if self.store.get('allow_bare', True) else 'пропускаются'}\n"
                'Куда отвечаем: на исходное сообщение в группе, от подключённого аккаунта.\n'
                f'Лимит: {self.config.max_per_minute} ответов в минуту.\n'
                'Остаток не уменьшается при предложении товара.')

    async def activate(self, message: Message):
        if not any(p.eligible for p in self.store.products()):
            await message.answer('Сначала загрузи прайс /price или список /catalog.')
        elif not self.store.get('price_ready', True):
            await message.answer('Дождись успешного обновления прайса: /refresh. Статус: /status')
        elif not self.store.chats():
            await message.answer('Сначала выбери группы: /chats')
        elif not self.client.is_connected():
            await message.answer('Нет соединения с аккаунтом Telegram. Ответы не включены.')
        else:
            self.store.set('active', True)
            await message.answer('▶️ Ответы включены. Обрабатываю новые запросы в выбранных группах. Остановить: /pause')

    async def show_chats(self, message: Message, page: int = 0, refresh: bool = False):
        if refresh or not self.dialogs_loaded:
            try:
                self.dialogs = [(d.id, d.title) async for d in self.client.iter_dialogs() if d.is_group]
                self.dialogs_loaded = True
            except FloodWaitError as exc:
                await message.answer(f'Telegram просит подождать {exc.seconds} секунд. Попробуй /chats позже.')
                return
            except (RPCError, OSError):
                await message.answer('Не удалось получить группы. Проверь соединение аккаунта и повтори /chats.')
                return
        page = min(max(0, page), max(0, (len(self.dialogs) - 1) // 8))
        selected = self.store.chats()
        rows = [[(f"{'✅' if cid in selected else '⬜'} {title[:44]}", f'toggle:{page}:{cid}')]
                for cid, title in self.dialogs[page*8:page*8+8]]
        nav = []
        if page:
            nav.append(('←', f'chats:{page-1}'))
        if (page+1)*8 < len(self.dialogs):
            nav.append(('→', f'chats:{page+1}'))
        if nav:
            rows.append(nav)
        rows.append([('🔄 Обновить', 'refresh'), ('Меню', 'home')])
        text = (f'💬 Группы: {len(self.dialogs)}. Выбрано: {len(selected)}. Страница {page+1}.\n'
                'Нажми на группу, чтобы включить или выключить её.\n'
                'Сообщения без «куплю»: /bare on или /bare off.')
        if not self.dialogs:
            text += '\nПодключённый аккаунт пока не состоит в группах.'
        await message.answer(text, reply_markup=keyboard(rows))

    async def show_test(self, message: Message, text: str):
        products = self.store.products()
        if not products:
            await message.answer('Сначала загрузи список: /catalog')
            return
        classification, found = await asyncio.to_thread(find_products, text, products, self.store.get('allow_bare', True))
        labels = {'buy': 'запрос на покупку', 'bare': 'название товара без «куплю»',
                  'sell': 'предложение продавца', 'uncertain': 'неопределённое сообщение'}
        text_out = f'Тип: {labels[classification]}\nСовпадений: {len(found)}\n'
        if found:
            text_out += '\nТакой ответ будет отправлен при включённых ответах:\n\n' + build_reply(found, self.store.get('intro'), self.config.contact)
        else:
            text_out += ('\nОтвета не будет. Проверь модель, версию, память, цвет, SIM и остаток. '
                         'Характеристика из запроса должна быть указана у товара. Для артикула заполни столбец «Артикул».')
        await message.answer(text_out[:4000], parse_mode=None)

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

        @dp.message(Command('start', 'help'))
        async def start(message: Message, state: FSMContext):
            await state.clear()
            await message.answer('Бот отвечает по актуальному прайсу в выбранных группах.\n\n'
                                 '1. /price — загрузить прайс от поставщика; /catalog — свой список.\n2. /chats — выбрать группы.\n'
                                 '3. /test — проверить запрос.\n4. /run — включить ответы.\n\n'
                                 '/pause — остановить; /status — статус; /stock — текущий список; '
                                 '/bare — запросы без «куплю»; /intro — начало ответа; /tail — последние отправки.', reply_markup=menu())

        @dp.message(Command('price'))
        async def price(message: Message, state: FSMContext):
            await state.set_state(Inputs.price)
            await state.update_data(price_parts=[])
            await message.answer('Пришли прайс с блоками «Название» и «От 1 шт — цена» текстом, несколькими сообщениями или файлом .txt.\n'
                                 'Если он разбит на части, дождись последней строки «Всего найдено N товаров». '
                                 'Без итоговой строки можно завершить кнопкой: тогда обновятся только присланные позиции.',
                                 reply_markup=keyboard([[('Завершить загрузку части прайса', 'price_done')]]))

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

        @dp.message(Command('refresh'))
        async def refresh(message: Message):
            if not self.price_sync:
                await message.answer('Автоисточник ещё не настроен. Нужны @username бота прайса и команда либо кнопка получения прайса.')
                return
            if self.price_sync.lock.locked():
                await message.answer('Обновление уже идёт.')
                return
            await message.answer('Запрашиваю новый прайс…')
            if await self.price_sync.refresh():
                await message.answer('Обновление завершено. Подробности: /changes.')

        @dp.message(Command('cancel'))
        async def cancel(message: Message, state: FSMContext):
            await state.clear()
            await message.answer('Ввод отменён.', reply_markup=menu())

        @dp.message(Command('catalog'))
        async def catalog(message: Message, state: FSMContext):
            await state.set_state(Inputs.catalog)
            await message.answer(CATALOG_PROMPT)

        @dp.message(Command('status'))
        async def status(message: Message):
            await message.answer(self.status(), reply_markup=menu())

        @dp.message(Command('chats'))
        async def chats(message: Message):
            await self.show_chats(message, refresh=True)

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
                await message.answer('Пришли пример запроса. Покажу результат без отправки в группы. Отмена: /cancel.')

        @dp.message(Command('bare'))
        async def bare(message: Message):
            mode = (message.text or '').partition(' ')[2].strip().casefold()
            if mode in {'on', 'off'}:
                self.store.set('allow_bare', mode == 'on')
            await message.answer(f"Названия без «куплю»: {'учитываются' if self.store.get('allow_bare', True) else 'пропускаются'}.\n"
                                 '/bare on — учитывать; /bare off — требовать намерение купить.')

        @dp.message(Command('intro'))
        async def intro(message: Message, state: FSMContext):
            await state.set_state(Inputs.intro)
            await message.answer('Пришли начало ответа, до 300 символов. Например: «Есть в наличии:». Отмена: /cancel.')

        @dp.message(Command('tail'))
        async def tail(message: Message):
            rows = self.store.recent()
            labels = {'sent': 'отправлено', 'failed': 'ошибка', 'uncertain': 'неизвестно, проверь в группе', 'pending': 'отправляется'}
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
                                              caption='Текущая версия прайса. Повторная загрузка — через /price. Остатки не указаны источником.')
                return
            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter='|', lineterminator='\n')
            writer.writerow(['Название', 'Цена', 'Остаток', 'Артикул'])
            for p in products:
                suffix = (' ' + (p.country or p.price_country)) if p.country or p.price_country else ''
                writer.writerow([p.name + suffix, str(p.price) if p.price is not None else '', p.stock if p.stock is not None else 'не указан', p.sku])
            await message.answer_document(BufferedInputFile(buffer.getvalue().encode('utf-8-sig'), filename='my_catalog.txt'))

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
            if action == 'catalog':
                await state.set_state(Inputs.catalog)
                await message.answer(CATALOG_PROMPT)
            elif action == 'price':
                await state.set_state(Inputs.price)
                await state.update_data(price_parts=[])
                await message.answer('Пришли все части прайса до строки «Всего найдено N товаров» или файлом .txt. Отмена: /cancel.',
                                     reply_markup=keyboard([[('Завершить загрузку части прайса', 'price_done')]]))
            elif action == 'price_done':
                if await state.get_state() != Inputs.price.state:
                    await message.answer('Сначала начни загрузку: /price')
                    return
                parts = (await state.get_data()).get('price_parts', [])
                await self.import_price(message, state, '\n'.join(parts))
            elif action == 'test':
                await state.set_state(Inputs.test)
                await message.answer('Пришли пример запроса. Сообщения в группы не отправляются. Отмена: /cancel.')
            elif action == 'status':
                await message.answer(self.status(), reply_markup=menu())
            elif action == 'home':
                await state.clear()
                await message.answer('Выбери действие:', reply_markup=menu())
            elif action == 'run':
                await self.activate(message)
            elif action == 'pause':
                self.store.set('active', False)
                await message.answer('⏸ Ответы выключены. Уже отправляемое сообщение может завершиться.', reply_markup=menu())
            elif action == 'refresh':
                await self.show_chats(message, refresh=True)
            elif action.startswith('chats:'):
                await self.show_chats(message, int(action.split(':')[1]))
            elif action.startswith('toggle:'):
                _, page, chat_id = action.split(':')
                title = dict(self.dialogs).get(int(chat_id))
                if title is None:
                    await message.answer('Список групп устарел. Обнови /chats.')
                    return
                self.store.toggle_chat(int(chat_id), title)
                try:
                    await message.edit_reply_markup(reply_markup=None)
                except TelegramAPIError:
                    pass
                await self.show_chats(message, int(page))

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
                await message.answer('Прайс больше 1 МБ. Начни заново: /price')
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
    session = load_string_session(config.telegram_session) if config.telegram_session else str(config.data_dir / 'account')
    store = Store(config.data_dir / 'catalog.sqlite3')
    client = TelegramClient(session, config.api_id, config.api_hash,
                            catch_up=False, flood_sleep_threshold=0, request_retries=0)
    bot = Bot(config.bot_token)
    tasks: list[asyncio.Task] = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError('Сессия Telegram не авторизована. Выполни login локально; для Railway также session-env. См. RAILWAY.md.')
        me = await client.get_me()
        if me.bot:
            raise ValueError('Для чтения чужих групп подключи свой пользовательский аккаунт через login')
        app = Application(config, store, client, bot)
        client.add_event_handler(app.monitor, events.NewMessage(incoming=True))
        if app.price_sync:
            client.add_event_handler(app.price_sync.receive, events.NewMessage(incoming=True))
            client.add_event_handler(app.price_sync.receive, events.MessageEdited(incoming=True))
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
            ('start', 'Меню'), ('price', 'Загрузить прайс'), ('changes', 'Изменения цен'),
            ('refresh', 'Обновить прайс из бота'), ('catalog', 'Загрузить список'), ('chats', 'Выбрать группы'),
            ('test', 'Проверить запрос'), ('run', 'Включить ответы'), ('pause', 'Пауза'),
            ('status', 'Статус'), ('stock', 'Скачать текущий список'), ('tail', 'Последние отправки')]])
        store.prune()
        await app.notify('Бот запущен. ' + app.status())
        tasks = [asyncio.create_task(app.dp.start_polling(bot, close_bot_session=False)),
                 asyncio.create_task(client.run_until_disconnected())]
        main_tasks = list(tasks)
        if app.price_sync:
            tasks.append(asyncio.create_task(app.price_sync.run()))
        done, _ = await asyncio.wait(main_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await client.disconnect()
        await bot.session.close()
        store.close()
