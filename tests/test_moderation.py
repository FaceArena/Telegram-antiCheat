import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatType

import moderation


def make_message():
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
        message_id=1001,
        media_group_id=None,
        text="Привет",
        caption=None,
        delete=AsyncMock(),
    )


def flood_settings():
    return {
        "antiflood": True,
        "stopwords": False,
        "flood_limit": 5,
        "flood_window": 10,
        "stopword_list": [],
        "punishment": "delete",
    }


class ModerationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixth_message_is_deleted_when_limit_is_five(self):
        message = make_message()
        with (
            patch.object(moderation.common, "pool", object(), create=True),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "record_flood_message", AsyncMock(return_value=6)),
            patch.object(moderation, "apply_punishment", AsyncMock()) as punish,
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            violated = await moderation.ModerationMiddleware()._violates(
                message, SimpleNamespace()
            )

        self.assertTrue(violated)
        message.delete.assert_awaited_once()
        punish.assert_awaited_once()

    async def test_flood_retries_punishment_after_transient_telegram_failure(self):
        message = make_message()
        with (
            patch.object(moderation.common, "pool", object(), create=True),
            patch.object(moderation, "get_settings", AsyncMock(return_value={
                **flood_settings(),
                "punishment": "mute",
            })),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(
                moderation,
                "record_flood_message",
                AsyncMock(side_effect=[6, 7]),
            ),
            patch.object(
                moderation,
                "apply_punishment",
                AsyncMock(side_effect=[moderation.TelegramAPIError("temporary"), None]),
            ) as punish,
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            middleware = moderation.ModerationMiddleware()
            await middleware._violates(message, SimpleNamespace())
            await middleware._violates(message, SimpleNamespace())

        self.assertEqual(punish.await_count, 2)
        self.assertEqual(message.delete.await_count, 2)

    async def test_message_at_limit_is_not_deleted(self):
        message = make_message()
        with (
            patch.object(moderation.common, "pool", object(), create=True),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "record_flood_message", AsyncMock(return_value=5)),
        ):
            violated = await moderation.ModerationMiddleware()._violates(
                message, SimpleNamespace()
            )

        self.assertFalse(violated)
        message.delete.assert_not_awaited()

    async def test_admin_message_is_exempt_from_antiflood(self):
        message = make_message()
        with (
            patch.object(moderation.common, "pool", object(), create=True),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=True)),
            patch.object(moderation, "record_flood_message", AsyncMock()) as record,
        ):
            violated = await moderation.ModerationMiddleware()._violates(
                message, SimpleNamespace()
            )

        self.assertFalse(violated)
        record.assert_not_awaited()
        message.delete.assert_not_awaited()

    async def test_database_error_does_not_create_false_positive(self):
        message = make_message()
        with (
            patch.object(moderation.common, "pool", object(), create=True),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(
                moderation,
                "record_flood_message",
                AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
        ):
            violated = await moderation.ModerationMiddleware()._violates(
                message, SimpleNamespace()
            )

        self.assertFalse(violated)
        message.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
