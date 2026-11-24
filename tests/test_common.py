import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

import common


class AdminCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        common._admin_cache.clear()
        common._admin_error_cache.clear()
        common._admin_cache_locks = tuple(asyncio.Lock() for _ in range(64))

    def tearDown(self):
        common._admin_cache.clear()
        common._admin_error_cache.clear()

    async def test_reuses_chat_snapshot_for_different_users(self):
        bot = SimpleNamespace(
            get_chat_administrators=AsyncMock(
                return_value=[
                    SimpleNamespace(user=SimpleNamespace(id=42)),
                ]
            )
        )
        with patch(
            "common.time.monotonic",
            side_effect=(10.0, 10.1, 10.2, 10.3),
        ):
            first = await common.is_chat_admin(bot, -1001, 42)
            second = await common.is_chat_admin(bot, -1001, 43)

        self.assertTrue(first)
        self.assertFalse(second)
        bot.get_chat_administrators.assert_awaited_once()
        self.assertEqual(common.ADMIN_CACHE_TTL, 5)

    async def test_refreshes_snapshot_after_ttl_expires(self):
        bot = SimpleNamespace(
            get_chat_administrators=AsyncMock(
                side_effect=[
                    [SimpleNamespace(user=SimpleNamespace(id=42))],
                    [SimpleNamespace(user=SimpleNamespace(id=43))],
                ]
            )
        )
        with patch(
            "common.time.monotonic",
            side_effect=(10.0, 10.1, 10.2, 16.0, 16.1, 16.2),
        ):
            first = await common.is_chat_admin(bot, -1001, 42)
            second = await common.is_chat_admin(bot, -1001, 42)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(bot.get_chat_administrators.await_count, 2)

    async def test_fresh_uses_get_chat_member_without_snapshot(self):
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(
                    status=ChatMemberStatus.ADMINISTRATOR
                )
            ),
            get_chat_administrators=AsyncMock(),
        )

        result = await common.is_chat_admin(bot, -1001, 42, fresh=True)

        self.assertTrue(result)
        bot.get_chat_member.assert_awaited_once_with(-1001, 42)
        bot.get_chat_administrators.assert_not_awaited()

    async def test_member_update_invalidates_only_affected_chat_snapshot(self):
        common._admin_cache[-1001] = (10.0, frozenset({42}))
        common._admin_cache[-1002] = (10.0, frozenset({43}))
        error = TelegramAPIError(method=SimpleNamespace(), message="temporary")
        common._admin_error_cache[-1001] = (100.0, error)
        common._admin_error_cache[-1002] = (100.0, error)

        await common.invalidate_admin_cache(-1001, 42)

        self.assertNotIn(-1001, common._admin_cache)
        self.assertIn(-1002, common._admin_cache)
        self.assertNotIn(-1001, common._admin_error_cache)
        self.assertIn(-1002, common._admin_error_cache)

    async def test_cache_size_is_bounded_and_oldest_entry_is_evicted(self):
        with patch("common.ADMIN_CACHE_MAX_ENTRIES", 2):
            with patch("common.time.monotonic", side_effect=(1.0, 2.0, 3.0)):
                common._store_admin_cache(-1001, {1})
                common._store_admin_cache(-1002, {2})
                common._store_admin_cache(-1003, {3})

        self.assertEqual(len(common._admin_cache), 2)
        self.assertNotIn(-1001, common._admin_cache)
        self.assertIn(-1002, common._admin_cache)
        self.assertIn(-1003, common._admin_cache)

    async def test_concurrent_misses_share_one_api_request(self):
        calls = 0

        async def get_admins(chat_id):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return [SimpleNamespace(user=SimpleNamespace(id=42))]

        bot = SimpleNamespace(get_chat_administrators=AsyncMock(side_effect=get_admins))

        results = await asyncio.gather(
            common.is_chat_admin(bot, -1001, 42),
            common.is_chat_admin(bot, -1001, 43),
        )

        self.assertEqual(results, [True, False])
        self.assertEqual(calls, 1)

    async def test_api_failure_uses_short_backoff_until_invalidation(self):
        error = TelegramAPIError(
            method=SimpleNamespace(),
            message="temporary Telegram outage",
        )
        bot = SimpleNamespace(
            get_chat_administrators=AsyncMock(side_effect=error)
        )

        with self.assertRaises(TelegramAPIError):
            await common.is_chat_admin(bot, -1001, 42)
        with self.assertRaises(TelegramAPIError):
            await common.is_chat_admin(bot, -1001, 43)

        bot.get_chat_administrators.assert_awaited_once()
        self.assertIn(-1001, common._admin_error_cache)

        await common.invalidate_admin_cache(-1001, 42)
        with self.assertRaises(TelegramAPIError):
            await common.is_chat_admin(bot, -1001, 42)

        self.assertEqual(bot.get_chat_administrators.await_count, 2)
        self.assertNotIn(-1001, common._admin_cache)

    async def test_temporary_group_notice_is_scheduled_for_four_second_deletion(self):
        sent = SimpleNamespace(message_id=987)
        bot = SimpleNamespace(send_message=AsyncMock(return_value=sent))
        with patch("common.schedule_temporary_delete") as schedule:
            result = await common.send_temporary_message(
                bot, -1001, "short notice", disable_notification=True
            )
        self.assertIs(result, sent)
        bot.send_message.assert_awaited_once_with(
            -1001, "short notice", disable_notification=True
        )
        schedule.assert_called_once_with(bot, -1001, 987, common.TEMP_NOTICE_SECONDS)
        self.assertEqual(common.TEMP_NOTICE_SECONDS, 4)

    async def test_invalidation_waits_for_inflight_snapshot_refresh(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def get_admins(chat_id):
            started.set()
            await release.wait()
            return [SimpleNamespace(user=SimpleNamespace(id=42))]

        bot = SimpleNamespace(get_chat_administrators=AsyncMock(side_effect=get_admins))
        lookup = asyncio.create_task(common.is_chat_admin(bot, -1001, 42))
        await started.wait()

        invalidation = asyncio.create_task(
            common.invalidate_admin_cache(-1001, 42)
        )
        await asyncio.sleep(0)
        self.assertFalse(invalidation.done())

        release.set()
        self.assertTrue(await lookup)
        await invalidation
        self.assertNotIn(-1001, common._admin_cache)


if __name__ == "__main__":
    unittest.main()
