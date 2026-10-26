"""Real Telethon cursor recovery without connecting to Telegram."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import TelegramClient, types
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from catalog_bot.group_directory import GroupDirectory
from catalog_bot.storage import Store
from catalog_bot.update_state import checkpoint_update_state, restore_channel_updates


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
ACCOUNT, CHANNEL, CHANNEL_HASH = 77, 1234567, 333444


def state(pts):
    return types.updates.State(pts=pts, qts=0, date=NOW, seq=0, unread_count=0)


def channel():
    return types.Channel(id=CHANNEL, title='Закупки', date=NOW, photo=types.ChatPhotoEmpty(),
                         access_hash=CHANNEL_HASH, megagroup=True)


class UpdateStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'account'
        self.clients = []
        previous = self.client()
        previous.session.auth_key = AuthKey(b'k' * 256)
        previous._mb_entity_cache.set_self_user(ACCOUNT, False, 777888)
        previous.session.process_entities(types.contacts.ResolvedPeer(
            peer=types.PeerUser(ACCOUNT), chats=[channel()],
            users=[types.User(id=ACCOUNT, access_hash=777888, first_name='Owner')]))
        previous._message_box.set_state(state(100))
        previous._message_box.try_set_channel_state(CHANNEL, 90)
        await checkpoint_update_state(previous)
        previous.session.close()

        # catch_up=False initializes a fresh account state at login, but does
        # not load persisted channel states. Mimic that startup locally.
        self.current = self.client()
        self.current._message_box.set_state(state(500))
        self.current._mb_entity_cache.set_self_user(ACCOUNT, False, 777888)

    def client(self):
        client = TelegramClient(SQLiteSession(str(self.path)), 123, 'test', catch_up=False)
        self.clients.append(client)
        return client

    async def asyncTearDown(self):
        for client in self.clients:
            client.session.close()
        self.temp.cleanup()

    async def test_restart_recovers_live_channel_gap_from_persisted_cursor(self):
        box, cache = self.current._message_box, self.current._mb_entity_cache
        update = types.UpdateChannelTooLong(channel_id=CHANNEL, pts=91)
        # Demonstrate the old behavior in the actual pinned SDK: it ignores
        # the gap completely when only the application group cache survived.
        self.assertIsNone(box.apply_pts_info(update, reset_deadlines=set()))
        self.assertIsNone(box.get_channel_difference(cache))

        restored = await restore_channel_updates(self.current, ACCOUNT)
        self.assertEqual(restored, 1)
        self.assertIsNone(box.apply_pts_info(update, reset_deadlines=set()))
        request = box.get_channel_difference(cache)
        self.assertEqual(request.channel.channel_id, CHANNEL)
        self.assertEqual(request.channel.access_hash, CHANNEL_HASH)
        self.assertEqual(request.pts, 90)

        message = types.Message(id=55, peer_id=types.PeerChannel(CHANNEL),
                                from_id=types.PeerUser(88), message='s25 navy', date=NOW)
        updates, _, _ = box.apply_channel_difference(request, types.updates.ChannelDifference(
            pts=91, new_messages=[message], other_updates=[], chats=[channel()],
            users=[types.User(id=88, first_name='Buyer', access_hash=123)], final=True), cache)
        messages = [update.message for update in updates if isinstance(update, types.UpdateNewChannelMessage)]
        self.assertEqual([message.message for message in messages], ['s25 navy'])

    async def test_restore_never_rewinds_live_account_or_channel_state(self):
        box = self.current._message_box
        box.try_set_channel_state(CHANNEL, 95)
        before, _ = box.session_state()
        restored = await restore_channel_updates(self.current, ACCOUNT)
        after, channels = box.session_state()
        self.assertEqual(restored, 0)
        self.assertEqual(after, before)
        self.assertEqual(after['pts'], 500)
        self.assertEqual(channels[CHANNEL], 95)

        # Basic groups use the live account cursor, untouched by restoration.
        update = types.UpdateNewMessage(message=types.Message(
            id=56, peer_id=types.PeerChat(99), from_id=types.PeerUser(88),
            message='S25 256 Navy', date=NOW), pts=501, pts_count=1)
        self.assertIs(box.apply_pts_info(update, reset_deadlines=set()), update)

    async def test_another_account_cannot_restore_previous_account_channels(self):
        self.assertEqual(await restore_channel_updates(self.current, 999), 0)
        self.assertEqual(self.current._message_box.session_state()[1], {})
        self.assertIsNone(self.current._mb_entity_cache.get(CHANNEL))

    async def test_switched_account_does_not_revive_old_cursors_on_second_restart(self):
        # Reusing the same client after a revoked key may leave old channel
        # state in memory as well as the persisted session.
        self.current._message_box.try_set_channel_state(CHANNEL, 95)
        self.current._message_box.try_begin_get_diff(CHANNEL, 'test old gap')
        self.current._mb_entity_cache.set_self_user(999, False, 888999)
        self.assertEqual(await restore_channel_updates(self.current, 999), 0)
        self.assertNotIn(CHANNEL, self.current._message_box.map)
        self.assertNotIn(CHANNEL, self.current._message_box.getting_diff_for)
        await checkpoint_update_state(self.current)
        self.current.session.close()

        following = self.client()
        following._message_box.set_state(state(700))
        following._mb_entity_cache.set_self_user(999, False, 888999)
        self.assertEqual(following.session.get_input_entity(0).access_hash, 999)
        self.assertEqual(following.session.get_update_state(CHANNEL).pts, 0)
        self.assertEqual(await restore_channel_updates(following, 999), 0)
        self.assertNotIn(CHANNEL, following._message_box.map)
        following._message_box.apply_pts_info(types.UpdateChannelTooLong(CHANNEL, pts=96), reset_deadlines=set())
        self.assertIsNone(following._message_box.get_channel_difference(following._mb_entity_cache))

    async def test_missing_marker_or_access_hash_does_not_invent_recovery_state(self):
        self.current.session.set_update_state(22222, state(10))
        self.assertEqual(await restore_channel_updates(self.current, ACCOUNT), 1)
        self.assertNotIn(22222, self.current._message_box.session_state()[1])

        fresh = TelegramClient(SQLiteSession(), 123, 'test', catch_up=False)
        self.clients.append(fresh)
        fresh.session.auth_key = AuthKey(b'z' * 256)
        fresh.session.set_update_state(CHANNEL, state(20))
        fresh._message_box.set_state(state(500))
        self.assertEqual(await restore_channel_updates(fresh, ACCOUNT), 0)
        self.assertEqual(fresh._message_box.session_state()[1], {})

    async def test_checkpoint_is_durable_before_sdk_periodic_keepalive(self):
        self.current._message_box.try_set_channel_state(CHANNEL, 99)
        self.assertTrue(await checkpoint_update_state(self.current))
        observer = SQLiteSession(str(self.path))
        try:
            self.assertEqual(observer.get_update_state(0).pts, 500)
            self.assertEqual(observer.get_update_state(CHANNEL).pts, 99)
            self.assertEqual(observer.get_input_entity(0).access_hash, ACCOUNT)
        finally:
            observer.close()

    async def test_empty_or_transport_only_clients_are_safe_before_login(self):
        self.assertEqual(await restore_channel_updates(SimpleNamespace(), ACCOUNT), 0)
        self.assertFalse(await checkpoint_update_state(SimpleNamespace()))
        fresh = TelegramClient(SQLiteSession(), 123, 'test', catch_up=False)
        self.clients.append(fresh)
        self.assertFalse(await checkpoint_update_state(fresh))
        self.assertIsNone(fresh.session.get_update_state(0))


class DirectoryCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.test_group_directory import ControlledSleep, DirectoryClient
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'catalog.sqlite3')
        self.store.set('telegram_account_id', ACCOUNT)
        self.client, self.wait = DirectoryClient(), ControlledSleep()
        self.available = asyncio.Event()
        self.available.set()
        self.directory = GroupDirectory(self.store, self.client, self.available, AsyncMock(),
                                        clock=self.wait.clock, sleep=self.wait.sleep, page_pause=0)

    async def asyncTearDown(self):
        task = self.directory.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    async def test_complete_scan_flushes_sdk_state_but_cached_browsing_does_not(self):
        with patch('catalog_bot.group_directory.checkpoint_update_state', new_callable=AsyncMock) as save:
            await self.directory.ensure_started()
            save.assert_awaited_once_with(self.client)
            self.assertIsNone(self.directory.ensure_started())
            self.assertIsNone(self.directory.ensure_started(refresh=True))
            save.assert_awaited_once()

    async def test_flood_pause_flushes_sdk_state_before_waiting(self):
        from telethon import errors
        self.client.failure = errors.FloodWaitError(None, capture=26)
        with patch('catalog_bot.group_directory.checkpoint_update_state', new_callable=AsyncMock) as save:
            task = self.directory.ensure_started()
            await asyncio.wait_for(self.wait.entered.wait(), 2)
            save.assert_awaited_once_with(self.client)
            self.wait.release.set()
            await task
            self.assertEqual(save.await_count, 2)

    async def test_checkpoint_failure_preserves_complete_group_cache(self):
        with patch('catalog_bot.group_directory.checkpoint_update_state',
                   new_callable=AsyncMock, side_effect=OSError('synthetic')) as save:
            with self.assertLogs('catalog_bot.group_directory', level='WARNING'):
                await self.directory.ensure_started()
            save.assert_awaited_once()
            self.assertTrue(self.directory.complete)
            self.assertEqual(len(self.directory.items), 2)

    async def test_cancelled_previous_account_does_not_flush_new_account_state(self):
        old_generation = self.directory.generation
        self.directory.bind(999)
        with patch('catalog_bot.group_directory.checkpoint_update_state', new_callable=AsyncMock) as save:
            await self.directory.checkpoint(old_generation)
            save.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
