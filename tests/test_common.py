import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatMemberStatus

import common


class AdminCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        common._admin_cache.clear()

    def tearDown(self):
        common._admin_cache.clear()

    async def test_reuses_result_inside_five_second_ttl(self):
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status=ChatMemberStatus.MEMBER)
            )
        )
        with patch("common.time.monotonic", side_effect=(10.0, 10.1, 10.2)):
            first = await common.is_chat_admin(bot, -1001, 42)
            second = await common.is_chat_admin(bot, -1001, 42)

        self.assertFalse(first)
        self.assertFalse(second)
        bot.get_chat_member.assert_awaited_once()
        self.assertEqual(common.ADMIN_CACHE_TTL, 5)

    async def test_refreshes_result_after_ttl_expires(self):
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                side_effect=[
                    SimpleNamespace(status=ChatMemberStatus.MEMBER),
                    SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),
                ]
            )
        )
        with patch("common.time.monotonic", side_effect=(10.0, 10.1, 16.0, 16.1)):
            first = await common.is_chat_admin(bot, -1001, 42)
            second = await common.is_chat_admin(bot, -1001, 42)

        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(bot.get_chat_member.await_count, 2)

    async def test_fresh_bypasses_cached_result(self):
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                side_effect=[
                    SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),
                    SimpleNamespace(status=ChatMemberStatus.MEMBER),
                ]
            )
        )
        with patch("common.time.monotonic", side_effect=(10.0, 10.1, 10.2, 10.3)):
            first = await common.is_chat_admin(bot, -1001, 42)
            second = await common.is_chat_admin(bot, -1001, 42, fresh=True)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(bot.get_chat_member.await_count, 2)
        self.assertFalse(common._admin_cache[(-1001, 42)][1])

    async def test_member_update_invalidates_one_cached_user(self):
        common._admin_cache[(-1001, 42)] = (10.0, True)
        common._admin_cache[(-1001, 43)] = (10.0, False)

        common.invalidate_admin_cache(-1001, 42)

        self.assertNotIn((-1001, 42), common._admin_cache)
        self.assertIn((-1001, 43), common._admin_cache)

    async def test_cache_size_is_bounded_and_oldest_entry_is_evicted(self):
        with patch("common.ADMIN_CACHE_MAX_ENTRIES", 2):
            with patch("common.time.monotonic", side_effect=(1.0, 2.0, 3.0)):
                common._store_admin_cache((-1001, 1), False)
                common._store_admin_cache((-1001, 2), True)
                common._store_admin_cache((-1001, 3), False)

        self.assertEqual(len(common._admin_cache), 2)
        self.assertNotIn((-1001, 1), common._admin_cache)
        self.assertIn((-1001, 2), common._admin_cache)
        self.assertIn((-1001, 3), common._admin_cache)


if __name__ == "__main__":
    unittest.main()
