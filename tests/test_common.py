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
        with patch("common.time.monotonic", side_effect=(10.0, 10.1)):
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
        with patch("common.time.monotonic", side_effect=(10.0, 16.0, 16.1)):
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
        with patch("common.time.monotonic", side_effect=(10.0, 10.1, 10.2)):
            first = await common.is_chat_admin(bot, -1001, 42)
            second = await common.is_chat_admin(bot, -1001, 42, fresh=True)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(bot.get_chat_member.await_count, 2)
        self.assertFalse(common._admin_cache[(-1001, 42)][1])


if __name__ == "__main__":
    unittest.main()
