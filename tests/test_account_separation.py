"""Owner-controller integration with independent supplier and seller accounts."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from catalog_bot.account_roles import prepare_account_roles
from catalog_bot.app import Application, Config
from catalog_bot.storage import Store
from tests import test_app as controller_helpers
from tests.test_account_login import AccountClient
from tests.test_price_sync import SourceClient


class RoleClient(AccountClient):
    def __init__(self, account_id, name):
        super().__init__()
        self.user = SimpleNamespace(id=account_id, username=name, first_name=name, bot=False)

    async def sign_in(self, **kwargs):
        user = await super().sign_in(**kwargs)
        # Distinct synthetic keys prove that persistence uses the right client.
        self.session.auth_key = AuthKey(bytes((index + self.user.id) % 256 for index in range(256)))
        self.session.save()
        return user


class PriceRoleClient(RoleClient, SourceClient):
    def __init__(self):
        RoleClient.__init__(self, 88, 'price_account')
        SourceClient.__init__(self)
        self.refreshed = asyncio.Event()

    async def send_message(self, peer, text, **kwargs):
        await SourceClient.send_message(self, peer, text, **kwargs)
        self.refreshed.set()


class AccountSeparationTests(unittest.IsolatedAsyncioTestCase):
    feed = controller_helpers.ControllerTests.feed
    texts = controller_helpers.ControllerTests.texts

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / 'catalog.sqlite3')
        prepare_account_roles(self.store, self.directory)
        self.transport = controller_helpers.LocalSession()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.transport)
        self.reply = RoleClient(77, 'reply_account')
        self.price = PriceRoleClient()
        self.config = Config(123, 'test', '', 55, self.directory,
                             price_bot='@tech_online_seller_bot', price_request='/prices')
        self.app = Application(self.config, self.store, self.reply, self.bot, price_client=self.price)
        self.price.sync = self.app.price_sync
        self.app.price_sync.timeout = .03
        self.update_id = 0
        self.tasks = []

    async def asyncTearDown(self):
        group_task = self.app.group_directory.cancel()
        if group_task:
            self.tasks.append(group_task)
        self.app.cancel_login()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.bot.session.close()
        self.reply.session.close()
        self.price.session.close()
        self.store.close()
        self.temp.cleanup()

    async def login(self, role, *, two_factor=False):
        client = self.price if role == 'price' else self.reply
        client.two_factor = two_factor
        await self.feed('/pricelogin' if role == 'price' else '/login')
        await self.feed('+79992223344' if role == 'price' else '+79991112233')
        await self.feed('1 2 3 4 5')
        if two_factor:
            self.assertIn('пароль 2FA', self.texts()[-1])
            await self.feed('synthetic-password')

    async def login_both_and_refresh(self):
        await self.login('reply')
        await self.login('price')
        await self.feed('/price')
        self.assertTrue(self.store.get('price_ready'))

    async def test_owner_phone_and_price_two_factor_login_persist_separate_accounts(self):
        await self.login('reply')
        self.assertTrue(self.app.auth.ready)
        self.assertFalse(self.app.price_auth.ready)
        self.store.set('active', True)
        await self.feed('/pricelogin')
        self.assertTrue(self.store.get('active'))
        self.assertTrue(self.app.auth.ready)
        self.assertTrue(self.app.account_available.is_set())
        self.price.two_factor = True
        await self.feed('+79992223344')
        await self.feed('1 2 3 4 5')
        self.assertFalse(self.app.price_auth.ready)
        self.assertTrue(self.app.auth.ready)
        self.assertIn('пароль 2FA', self.texts()[-1])
        await self.feed('synthetic-password')
        self.assertTrue(self.app.price_auth.ready)
        self.assertTrue(self.app.price_account_available.is_set())
        self.assertTrue(self.store.get('active'))
        self.assertEqual(self.reply.phone_calls, ['+79991112233'])
        self.assertEqual(self.price.phone_calls, ['+79992223344'])
        self.assertEqual(self.store.get('telegram_account_id'), 77)
        self.assertEqual(self.store.get('price_account_id'), 88)
        self.assertEqual(self.store.get('telegram_account_name'), 'reply_account')
        self.assertEqual(self.store.get('price_account_name'), 'price_account')
        reply_path = self.directory / 'replies' / 'account.session'
        price_path = self.directory / 'account.session'
        self.assertEqual(self.app.auth.data_dir, reply_path.parent)
        self.assertEqual(self.app.price_auth.data_dir, price_path.parent)
        reply_session = SQLiteSession(str(reply_path))
        price_session = SQLiteSession(str(price_path))
        try:
            self.assertEqual(reply_session.auth_key.key, self.reply.session.auth_key.key)
            self.assertEqual(price_session.auth_key.key, self.price.session.auth_key.key)
            self.assertNotEqual(reply_session.auth_key.key, price_session.auth_key.key)
        finally:
            reply_session.close()
            price_session.close()
        self.assertEqual(reply_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(price_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-password', '\n'.join(self.texts()))

    async def test_new_seller_login_preserves_ready_supplier_peer_and_price_deadline(self):
        await self.login('price')
        await self.feed('/price')
        peer = self.app.price_sync.peer
        self.assertIsNotNone(peer)
        self.app.price_schedule.next_at = 7777
        self.app.refresh_requested.clear()
        await self.login('reply')
        self.assertTrue(self.store.get('price_ready'))
        self.assertIs(self.app.price_sync.peer, peer)
        self.assertEqual(self.app.price_schedule.next_at, 7777)
        self.assertFalse(self.app.refresh_requested.is_set())
        self.assertEqual(self.price.calls, ['/prices'])
        self.assertTrue(self.app.price_auth.ready)

    async def test_price_command_uses_only_price_client_without_seller_login(self):
        await self.login('price')
        self.assertFalse(self.app.auth.ready)
        self.assertFalse(self.app.account_available.is_set())
        await self.feed('/price')
        self.assertIs(self.app.price_sync.client, self.price)
        self.assertEqual(self.price.calls, ['/prices'])
        self.assertTrue(self.store.get('price_ready'))
        self.assertEqual(str(self.store.products()[0].price), '85000')
        self.assertFalse(self.reply.connected)
        self.assertIn('Прайс обновлён', self.texts()[-1])

    async def test_price_worker_waits_for_price_availability_instead_of_seller(self):
        self.app.price_schedule.mark_due()
        self.tasks.append(asyncio.create_task(self.app.price_loop()))
        self.app.account_available.set()
        await asyncio.sleep(0)
        self.assertEqual(self.price.calls, [])
        await self.login('price')
        await asyncio.wait_for(self.price.refreshed.wait(), 1)
        self.assertEqual(self.price.calls, ['/prices'])
        self.assertTrue(self.store.get('price_ready'))
        self.assertFalse(self.app.auth.ready)

    async def test_reply_disconnect_keeps_price_session_and_ready_prices(self):
        await self.login_both_and_refresh()
        peer = self.app.price_sync.peer
        self.app.account_unavailable(authorization_lost=True)
        self.assertFalse(self.app.auth.ready)
        self.assertFalse(self.app.account_available.is_set())
        self.assertTrue(self.app.price_auth.ready)
        self.assertTrue(self.app.price_account_available.is_set())
        self.assertTrue(self.store.get('price_ready'))
        self.assertIs(self.app.price_sync.peer, peer)
        await self.feed('/price')
        self.assertEqual(self.price.calls, ['/prices', '/prices'])

    async def test_price_disconnect_does_not_disconnect_or_pause_the_reply_account(self):
        await self.login_both_and_refresh()
        self.store.set('active', True)
        self.app.price_account_unavailable(authorization_lost=True)
        self.assertFalse(self.app.price_auth.ready)
        self.assertFalse(self.app.price_account_available.is_set())
        self.assertFalse(self.store.get('price_ready'))
        self.assertTrue(self.app.auth.ready)
        self.assertTrue(self.app.account_available.is_set())
        self.assertTrue(self.store.get('active'))
        self.assertEqual(self.store.get('telegram_account_id'), 77)

    async def test_price_account_restore_loop_does_not_connect_or_authorize_seller(self):
        await self.price.sign_in(code='12345')
        self.tasks.append(asyncio.create_task(self.app.price_account_loop()))
        await asyncio.wait_for(self.app.price_account_available.wait(), 1)
        self.assertTrue(self.app.price_auth.ready)
        self.assertEqual(self.store.get('price_account_id'), 88)
        self.assertFalse(self.reply.connected)
        self.assertFalse(self.app.auth.ready)
        self.assertFalse(self.app.account_available.is_set())

    async def test_status_distinguishes_both_accounts_and_their_connections(self):
        await self.login_both_and_refresh()
        await self.feed('/status')
        text = self.texts()[-1]
        self.assertIn('reply_account', text)
        self.assertIn('Аккаунт прайса: price_account', text)
        self.assertIn('Соединение прайса: есть', text)
        self.app.price_account_unavailable(authorization_lost=True)
        await self.feed('/status')
        self.assertIn('reply_account', self.texts()[-1])
        self.assertIn('/priceloginqr', self.texts()[-1])
        self.assertIn('Соединение прайса: нет', self.texts()[-1])

    async def test_price_login_is_owner_private_only(self):
        await self.feed('/pricelogin', user_id=66)
        await self.feed('/pricelogin', chat_type='group')
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.price.connected)
        await self.feed('/pricelogin')
        await self.feed('+79992223344', user_id=66)
        await self.feed('+79992223344', chat_type='group')
        self.assertEqual(self.price.phone_calls, [])
        self.assertEqual(self.reply.phone_calls, [])

    async def test_price_login_command_can_replace_a_pending_seller_password_flow(self):
        self.reply.two_factor = True
        await self.feed('/login')
        await self.feed('+79991112233')
        await self.feed('1 2 3 4 5')
        self.assertEqual(self.app.auth.phase, 'password')
        await self.login('price')
        self.assertTrue(self.app.price_auth.ready)
        self.assertFalse(self.app.auth.ready)
        self.assertEqual(self.app.auth.phase, '')
        self.assertTrue(all('password' not in call for call in self.reply.sign_calls))


if __name__ == '__main__':
    unittest.main()
