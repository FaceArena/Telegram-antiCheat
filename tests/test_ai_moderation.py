import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ai_guard
import moderation
from tests.test_moderation import make_message, flood_settings


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
