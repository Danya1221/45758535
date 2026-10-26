"""Exercise real Telethon event dispatch and owner controls without a network."""
import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from telethon import TelegramClient, errors, events, types
from telethon.sessions import MemorySession

from catalog_bot.app import Application, Config
from catalog_bot.catalog import parse_catalog
from catalog_bot.storage import Store
from tests import test_app as helpers


class DispatchClient(TelegramClient):
    def __init__(self):
        super().__init__(MemorySession(), 123, 'test', catch_up=False)
        self._mb_entity_cache.set_self_user(77, False, 700)
        self.send_message = AsyncMock()
        self.set_receive_updates = AsyncMock()
        self.get_messages = AsyncMock(return_value=[])
        self.get_entity = AsyncMock(side_effect=OSError('offline sender lookup'))
        self.get_input_entity = AsyncMock(side_effect=self.private_recipient)

    @staticmethod
    async def private_recipient(peer):
        if not isinstance(peer, types.PeerUser) or peer.user_id <= 0:
            raise ValueError('Expected an individual user')
        return types.InputPeerUser(peer.user_id, 800)

    def is_connected(self):
        return True


class MonitorFlowTests(unittest.IsolatedAsyncioTestCase):
    feed = helpers.ControllerTests.feed
    texts = helpers.ControllerTests.texts

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'catalog.sqlite3')
        self.transport = helpers.LocalSession()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.transport)
        self.client = DispatchClient()
        self.app = Application(Config(123, 'test', '', 55, Path(self.temp.name)), self.store, self.client, self.bot)
        self.app.auth.ready = True
        self.app.account_available.set()
        self.store.set('telegram_account_id', 77)
        self.store.replace_products(parse_catalog('Galaxy S25 12/256 Navy SM-S931B|49000|1\nGalaxy S25 Ultra 12/256 Black|66500|1'))
        self.store.toggle_chat(-123, 'Тест')
        self.store.set('active', True)
        self.client.add_event_handler(self.app.monitor, events.NewMessage())
        self.update_id = 1000

    async def asyncTearDown(self):
        task = self.app.group_directory.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await self.bot.session.close()
        self.store.close()
        self.temp.cleanup()

    def update(self, text='s25 navy', *, number=28, outgoing=False, channel=False, sender=True, age=0, from_peer=None):
        peer = types.PeerChannel(123) if channel else types.PeerChat(123)
        author = from_peer or types.PeerUser(77 if outgoing else 88)
        message = types.Message(id=number, peer_id=peer, from_id=author,
                                date=datetime.fromtimestamp(int(time.time()) - age, timezone.utc),
                                message=text, out=outgoing)
        cls = types.UpdateNewChannelMessage if channel else types.UpdateNewMessage
        update = cls(message=message, pts=20, pts_count=1)
        update._entities = ({author.user_id: types.User(id=author.user_id, first_name='Buyer', access_hash=800, bot=False)}
                            if sender and isinstance(author, types.PeerUser) else {})
        return update

    async def test_basic_group_incoming_navy_sends_private_quote_to_author(self):
        await self.client._dispatch_update(self.update())
        self.client.send_message.assert_awaited_once()
        args = self.client.send_message.await_args
        self.assertEqual(args.args[0], types.InputPeerUser(88, 800))
        self.client.get_input_entity.assert_awaited_once_with(types.PeerUser(88))
        self.assertNotIn('reply_to', args.kwargs)
        self.assertIn('Galaxy S25 12/256 Navy', args.args[1])
        self.assertIn('49 000 ₽', args.args[1])
        self.assertNotIn('Ultra', args.args[1])
        self.assertEqual(self.store.message_check(-123, 28)['result'], 'sent')

    async def test_megagroup_also_sends_to_user_and_keeps_source_in_history(self):
        self.store.toggle_chat(-1000000000123, 'Мегагруппа')
        await self.client._dispatch_update(self.update(channel=True))
        self.assertEqual(self.client.send_message.await_args.args[0], types.InputPeerUser(88, 800))
        self.assertNotIn('reply_to', self.client.send_message.await_args.kwargs)
        self.assertEqual(self.store.message_check(-1000000000123, 28)['result'], 'sent')
        self.assertEqual(self.store.delivery(-1000000000123, 28)['sender_id'], 88)

    async def test_own_and_missing_black_do_not_block_next_incoming_navy(self):
        await self.client._dispatch_update(self.update('S25 256 black', number=1, outgoing=True))
        await self.client._dispatch_update(self.update('S25 256 black', number=2))
        self.client.send_message.assert_not_awaited()
        self.assertEqual(self.store.message_check(-123, 1)['result'], 'own_message')
        self.assertEqual(self.store.message_check(-123, 2)['result'], 'no_match')
        await self.client._dispatch_update(self.update(number=3))
        self.client.send_message.assert_awaited_once()

    async def test_sender_lookup_failure_skips_one_message_without_pausing_groups(self):
        await self.client._dispatch_update(self.update(sender=False))
        self.assertEqual(self.store.message_check(-123, 28)['result'], 'sender_unavailable')
        self.assertTrue(self.store.get('active'))
        self.client.send_message.assert_not_awaited()
        await self.client._dispatch_update(self.update(number=29))
        self.client.send_message.assert_awaited_once()

    async def test_activation_accepts_new_message_in_same_telegram_second(self):
        self.store.set('active', False)
        stamp = int(time.time())
        with patch('catalog_bot.app.time.time', return_value=stamp + .9):
            await self.feed('/run')
        self.client.set_receive_updates.assert_awaited_once_with(True)
        update = self.update()
        update.message.date = datetime.fromtimestamp(stamp, timezone.utc)
        await self.client._dispatch_update(update)
        self.client.send_message.assert_awaited_once()

    async def test_activation_failure_cannot_report_enabled(self):
        self.store.set('active', False)
        self.client.set_receive_updates.side_effect = errors.RPCError(None, 'private error body', 400)
        await self.feed('/run')
        self.assertFalse(self.store.get('active'))
        self.assertIn('Не удалось включить получение', self.texts()[-1])
        self.assertNotIn('private error body', self.texts()[-1])

    async def test_before_enable_and_stale_messages_never_send(self):
        self.store.set('monitor_enabled_at', int(time.time()) - 10)
        await self.client._dispatch_update(self.update(age=11))
        self.assertEqual(self.store.message_check(-123, 28)['result'], 'before_enable')
        self.store.set('monitor_enabled_at', 0)
        await self.client._dispatch_update(self.update(number=29, age=121))
        self.assertEqual(self.store.message_check(-123, 29)['result'], 'old')
        self.client.send_message.assert_not_awaited()

    async def test_why_command_is_read_only_and_owner_private_only(self):
        update = self.update()
        self.client.get_messages.return_value = [update.message]
        await self.feed('/why', user_id=66)
        await self.feed('/why', chat_type='group')
        self.client.get_messages.assert_not_awaited()
        await self.feed('/why')
        self.client.get_messages.assert_awaited_once()
        self.client.send_message.assert_not_awaited()
        self.assertEqual(self.store.recent(), [])
        self.assertIn('s25 navy', self.texts()[-1])

    async def test_forged_why_callback_does_not_read_unselected_chat(self):
        message = Message(message_id=2, date=datetime.now(timezone.utc), chat=Chat(id=55, type='private'))
        callback = CallbackQuery(id='why', from_user=User(id=55, is_bot=False, first_name='Owner'),
                                 chat_instance='test', message=message, data='why_chat:-999')
        await self.app.dp.feed_update(self.bot, Update(update_id=2000, callback_query=callback))
        self.client.get_messages.assert_not_awaited()
        self.assertIn('не выбрана', self.texts()[-1])

    async def test_status_shows_only_currently_selected_groups(self):
        self.store.record_check(-123, 28, 'no_match')
        self.store.record_check(-999, 29, 'sent')
        self.assertIn('Тест', self.app.monitor_status())
        self.assertNotIn('-999', self.app.monitor_status())
        self.store.toggle_chat(-123, 'Тест')
        self.assertIn('пока не получены', self.app.monitor_status())

    async def test_closed_private_messages_never_fall_back_to_group_or_pause_others(self):
        self.client.send_message.side_effect = errors.UserPrivacyRestrictedError(None)
        await self.client._dispatch_update(self.update())
        self.assertEqual(self.client.send_message.await_count, 1)
        self.assertIsInstance(self.client.send_message.await_args.args[0], types.InputPeerUser)
        self.assertEqual(self.store.delivery(-123, 28)['status'], 'failed')
        self.assertTrue(self.store.get('active'))
        self.assertIn('Ответы другим пользователям продолжаются', self.texts()[-1])
        self.assertNotIn('приостановлены', self.texts()[-1])
        self.client.send_message.side_effect = None
        update = self.update(number=29, from_peer=types.PeerUser(99))
        await self.client._dispatch_update(update)
        self.assertEqual(self.client.send_message.await_count, 2)
        self.assertEqual(self.client.send_message.await_args.args[0].user_id, 99)

    async def test_wrong_recipient_type_cannot_send_into_group(self):
        self.client.get_input_entity.side_effect = None
        self.client.get_input_entity.return_value = types.InputPeerChat(123)
        await self.client._dispatch_update(self.update())
        self.client.send_message.assert_not_awaited()
        self.assertEqual(self.store.delivery(-123, 28)['detail'], 'recipient_not_user')
        self.assertTrue(self.store.get('active'))

    async def test_anonymous_channel_sender_cannot_be_private_recipient(self):
        update = self.update(from_peer=types.PeerChannel(12345))
        await self.client._dispatch_update(update)
        self.client.send_message.assert_not_awaited()
        self.client.get_input_entity.assert_not_awaited()
        self.assertEqual(self.store.message_check(-123, 28)['result'], 'missing_sender')

    async def test_private_flood_wait_pauses_without_any_group_fallback(self):
        self.client.send_message.side_effect = errors.FloodWaitError(None, capture=25)
        await self.client._dispatch_update(self.update())
        self.assertFalse(self.store.get('active'))
        self.assertEqual(self.store.delivery(-123, 28)['detail'], 'flood_wait_25s')
        self.assertEqual(self.client.send_message.await_count, 1)
        self.assertIsInstance(self.client.send_message.await_args.args[0], types.InputPeerUser)
