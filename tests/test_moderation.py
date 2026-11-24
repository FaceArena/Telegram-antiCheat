import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError

import moderation
from flood import LocalFloodGuard


def make_message(message_id=1001):
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type=ChatType.SUPERGROUP),
        sender_chat=None,
        is_automatic_forward=False,
        new_chat_members=None,
        left_chat_member=None,
        from_user=SimpleNamespace(
            id=456,
            is_bot=False,
            username="member",
            full_name="Member",
        ),
        message_id=message_id,
        media_group_id=None,
        text="Привет",
        caption=None,
        delete=AsyncMock(),
    )


def flood_settings():
    return {
        "antiflood": False,  # базовая защита обязана работать и при выключенном тумблере
        "stopwords": False,
        "flood_limit": 5,
        "flood_window": 10,
        "stopword_list": [],
        "punishment": "delete",
    }


class LocalFloodGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_messages_and_mutes_after_three_over_limit_messages(self):
        guard = LocalFloodGuard()
        results = []
        for message_id in range(1, 6):
            results.append(await guard.record(
                chat_id=-1001, user_id=42, message_id=message_id,
                media_group_id=None, limit=2, window=5,
            ))
        self.assertEqual(results[0], (1, False, False))
        self.assertEqual(results[1], (2, False, False))
        self.assertEqual(results[2], (3, True, False))
        self.assertEqual(results[3], (4, True, False))
        self.assertEqual(results[4], (5, True, True))

    async def test_duplicate_update_does_not_increase_counter(self):
        guard = LocalFloodGuard()
        first = await guard.record(
            chat_id=-1001, user_id=42, message_id=10,
            media_group_id=None, limit=1, window=5,
        )
        duplicate = await guard.record(
            chat_id=-1001, user_id=42, message_id=10,
            media_group_id=None, limit=1, window=5,
        )
        self.assertEqual(first, (1, False, False))
        self.assertEqual(duplicate, (1, False, False))


class ModerationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixth_message_is_deleted_even_when_antiflood_toggle_is_off(self):
        message = make_message()
        guard = AsyncMock()
        guard.record.return_value = (6, True, False)
        guard.recent_message_ids.return_value = [1001, 1002, 1003, 1004, 1005, 1006]
        bot = SimpleNamespace(delete_messages=AsyncMock())
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "apply_punishment", AsyncMock()) as punish,
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            violated = await moderation.ModerationMiddleware()._violates(
                message, bot
            )

        self.assertTrue(violated)
        bot.delete_messages.assert_awaited_once_with(
            -100123, [1001, 1002, 1003, 1004, 1005, 1006]
        )
        guard.recent_message_ids.assert_awaited_once_with(-100123, 456)
        guard.record.assert_awaited_once()
        punish.assert_awaited_once_with(
            unittest.mock.ANY, -100123, 456, "delete"
        )

    async def test_third_over_limit_message_causes_automatic_mute(self):
        messages = [make_message(1001), make_message(1002), make_message(1003)]
        guard = AsyncMock()
        guard.record.side_effect = [
            (6, True, False),
            (7, True, False),
            (8, True, True),
        ]
        guard.recent_message_ids.return_value = [1001, 1002, 1003]
        bot = SimpleNamespace(delete_messages=AsyncMock())
        punish = AsyncMock()
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "apply_punishment", punish),
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            middleware = moderation.ModerationMiddleware()
            for message in messages:
                await middleware._violates(message, bot)

        self.assertEqual(bot.delete_messages.await_count, 3)
        self.assertEqual(punish.await_count, 3)
        self.assertEqual(punish.await_args_list[-1].args[-1], "mute")

    async def test_admin_is_exempt(self):
        message = make_message()
        guard = AsyncMock()
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=True)),
        ):
            violated = await moderation.ModerationMiddleware()._violates(
                message, SimpleNamespace()
            )

        self.assertFalse(violated)
        guard.record.assert_not_awaited()
        message.delete.assert_not_awaited()

    async def test_settings_database_error_does_not_disable_baseline_guard(self):
        message = make_message()
        guard = AsyncMock()
        guard.record.return_value = (6, True, False)
        guard.recent_message_ids.return_value = [1001, 1002, 1003, 1004, 1005, 1006]
        bot = SimpleNamespace(delete_messages=AsyncMock())
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(side_effect=RuntimeError("db down"))),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "apply_punishment", AsyncMock()),
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            violated = await moderation.ModerationMiddleware()._violates(
                message, bot
            )

        self.assertTrue(violated)
        bot.delete_messages.assert_awaited_once()
        guard.record.assert_awaited_once()

    async def test_admin_check_fallback_is_used(self):
        message = make_message()
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member"))
        )
        guard = AsyncMock()
        guard.record.return_value = (1, False, False)
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(
                moderation, "is_chat_admin",
                AsyncMock(side_effect=TelegramForbiddenError(
                    method=SimpleNamespace(), message="temporary"
                )),
            ),
        ):
            violated = await moderation.ModerationMiddleware()._violates(message, bot)

        self.assertFalse(violated)
        bot.get_chat_member.assert_awaited_once_with(-100123, 456)
        guard.record.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
