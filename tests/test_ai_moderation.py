import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ai_guard
import moderation
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError


def make_message():
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type=ChatType.SUPERGROUP),
        sender_chat=None, is_automatic_forward=False,
        new_chat_members=None, left_chat_member=None,
        from_user=SimpleNamespace(id=456, is_bot=False, username="member", full_name="Member"),
        message_id=1001, media_group_id=None, text="Проверьте аккаунт по ссылке", caption=None,
        entities=[], caption_entities=[], delete=AsyncMock(),
    )


def flood_settings():
    return {
        "antiflood": False, "stopwords": False, "flood_limit": 5,
        "flood_window": 5, "stopword_list": [], "punishment": "delete",
    }


class AIModerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_high_confidence_malicious_message_is_deleted_and_logged(self):
        message = make_message()
        bot = SimpleNamespace()
        result = ai_guard.AIResult("malicious", "phishing", 0.97, "Поддельная форма входа")
        guard = AsyncMock()
        guard.record.return_value = (1, False, None)
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(ai_guard, "analyze_message", AsyncMock(return_value=result)),
            patch.object(ai_guard, "record_violation", AsyncMock(return_value=(1, "delete"))),
            patch.object(moderation, "write_log", AsyncMock()) as log,
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            violated = await moderation.ModerationMiddleware()._violates(message, bot)

        self.assertTrue(violated)
        message.delete.assert_awaited_once()
        self.assertTrue(any(call.args[2] == "ai_guard_violation" for call in log.await_args_list))

    async def test_ai_uses_group_mute_setting_even_when_deletion_fails(self):
        message = make_message()
        message.delete.side_effect = TelegramForbiddenError(
            method=SimpleNamespace(), message="delete permission missing"
        )
        bot = SimpleNamespace()
        result = ai_guard.AIResult("malicious", "phishing", 0.97, "Поддельная форма входа")
        guard = AsyncMock()
        guard.record.return_value = (1, False, None)
        punish = AsyncMock()
        settings = flood_settings()
        settings["punishment"] = "mute"
        notify = AsyncMock()
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=settings)),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(ai_guard, "analyze_message", AsyncMock(return_value=result)),
            patch.object(ai_guard, "record_violation", AsyncMock(return_value=(1, "ban"))),
            patch.object(moderation, "apply_punishment", punish),
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", notify),
        ):
            violated = await moderation.ModerationMiddleware()._violates(message, bot)

        self.assertTrue(violated)
        punish.assert_awaited_once_with(bot, -100123, 456, "mute")
        self.assertEqual(notify.await_args.kwargs.get("source"), "AI")

    async def test_ai_uses_group_ban_setting_not_ai_escalation(self):
        message = make_message()
        bot = SimpleNamespace()
        result = ai_guard.AIResult("malicious", "scam", 0.99, "Мошенническое предложение")
        guard = AsyncMock()
        guard.record.return_value = (5, False, None)
        settings = flood_settings()
        settings["punishment"] = "ban"
        punish = AsyncMock()
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=settings)),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(ai_guard, "analyze_message", AsyncMock(return_value=result)),
            patch.object(ai_guard, "record_violation", AsyncMock(return_value=(5, "mute"))),
            patch.object(moderation, "apply_punishment", punish),
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            violated = await moderation.ModerationMiddleware()._violates(message, bot)

        self.assertTrue(violated)
        punish.assert_awaited_once_with(bot, -100123, 456, "ban")

    async def test_low_confidence_malicious_message_is_not_auto_deleted(self):
        message = make_message()
        bot = SimpleNamespace()
        result = ai_guard.AIResult("malicious", "scam", 0.84, "Недостаточно доказательств")
        guard = AsyncMock()
        guard.record.return_value = (1, False, None)
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(ai_guard, "analyze_message", AsyncMock(return_value=result)),
            patch.object(ai_guard, "record_violation", AsyncMock()) as record,
            patch.object(moderation, "write_log", AsyncMock()) as log,
        ):
            violated = await moderation.ModerationMiddleware()._violates(message, bot)

        self.assertFalse(violated)
        message.delete.assert_not_awaited()
        record.assert_not_awaited()
        self.assertTrue(any(call.args[2] == "ai_guard_review" for call in log.await_args_list))


if __name__ == "__main__":
    unittest.main()
