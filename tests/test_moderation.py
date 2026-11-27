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
    async def test_album_is_one_flood_event_but_all_parts_are_deletable(self):
        guard = LocalFloodGuard()
        first = await guard.record(
            chat_id=-1001, user_id=42, message_id=101,
            media_group_id="album-1", limit=5, window=5,
        )
        second = await guard.record(
            chat_id=-1001, user_id=42, message_id=102,
            media_group_id="album-1", limit=5, window=5,
        )
        self.assertEqual(first, (1, False, None))
        self.assertEqual(second, (1, False, None))
        self.assertEqual(
            await guard.recent_message_ids(-1001, 42),
            [101, 102],
        )

    async def test_sixth_message_immediately_requests_mute(self):
        guard = LocalFloodGuard()
        results = []
        for message_id in range(1, 7):
            results.append(await guard.record(
                chat_id=-1001, user_id=42, message_id=message_id,
                media_group_id=None, limit=5, window=5,
            ))
        self.assertEqual(results[4], (5, False, None))
        self.assertEqual(results[5], (6, True, "mute"))

    async def test_extreme_burst_requests_ban(self):
        guard = LocalFloodGuard()
        results = []
        for message_id in range(1, 11):
            results.append(await guard.record(
                chat_id=-1001, user_id=42, message_id=message_id,
                media_group_id=None, limit=5, window=5,
            ))
        self.assertEqual(results[5][2], "mute")
        self.assertEqual(results[9], (10, True, "ban"))

    async def test_three_identical_messages_are_spam_even_below_rate_limit(self):
        guard = LocalFloodGuard()
        actions = []
        for message_id in range(1, 4):
            result = await guard.record(
                chat_id=-1001, user_id=42, message_id=message_id,
                media_group_id=None, limit=5, window=5,
                fingerprint="одинаковый текст",
            )
            actions.append(result[2])
        self.assertEqual(actions, [None, None, "mute"])

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
        self.assertEqual(first, (1, False, None))
        self.assertEqual(duplicate, (1, False, None))

    async def test_failed_punishment_can_be_retried(self):
        guard = LocalFloodGuard()
        first = None
        for message_id in range(1, 7):
            first = await guard.record(
                chat_id=-1001, user_id=42, message_id=message_id,
                media_group_id=None, limit=5, window=5,
            )
        self.assertEqual(first[2], "mute")
        await guard.punishment_failed(-1001, 42, "mute")
        retry = await guard.record(
            chat_id=-1001, user_id=42, message_id=7,
            media_group_id=None, limit=5, window=5,
        )
        self.assertEqual(retry[2], "mute")

    async def test_recent_message_ids_returns_entire_burst(self):
        guard = LocalFloodGuard()
        for message_id in range(1, 7):
            await guard.record(
                chat_id=-1001, user_id=42, message_id=message_id,
                media_group_id=None, limit=5, window=5,
            )
        self.assertEqual(
            await guard.recent_message_ids(-1001, 42),
            [1, 2, 3, 4, 5, 6],
        )


class ModerationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixth_message_is_deleted_even_when_antiflood_toggle_is_off(self):
        message = make_message()
        guard = AsyncMock()
        guard.record.return_value = (6, True, "mute")
        guard.recent_message_ids.return_value = [1001, 1002, 1003, 1004, 1005, 1006]
        bot = SimpleNamespace(delete_messages=AsyncMock())
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value={**flood_settings(), "punishment": "mute"})),
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
            unittest.mock.ANY, -100123, 456, "mute"
        )

    async def test_first_over_limit_message_causes_immediate_mute(self):
        messages = [make_message(1001), make_message(1002), make_message(1003)]
        guard = AsyncMock()
        guard.record.side_effect = [
            (6, True, "mute"),
            (7, True, None),
            (8, True, None),
        ]
        guard.recent_message_ids.return_value = [1001, 1002, 1003]
        bot = SimpleNamespace(delete_messages=AsyncMock())
        punish = AsyncMock()
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value={**flood_settings(), "punishment": "mute"})),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "apply_punishment", punish),
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            middleware = moderation.ModerationMiddleware()
            for message in messages:
                await middleware._violates(message, bot)

        self.assertEqual(bot.delete_messages.await_count, 3)
        self.assertEqual(punish.await_count, 1)
        self.assertEqual(punish.await_args_list[0].args[-1], "mute")

    async def test_repeated_text_log_uses_the_correct_reason(self):
        message = make_message()
        guard = AsyncMock()
        guard.record.return_value = (3, True, "mute")
        guard.recent_message_ids.return_value = [1001, 1002, 1003]
        bot = SimpleNamespace(delete_messages=AsyncMock())
        write_log = AsyncMock()
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "apply_punishment", AsyncMock()),
            patch.object(moderation, "write_log", write_log),
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            await moderation.ModerationMiddleware()._violates(message, bot)

        write_log.assert_awaited_once_with(
            -100123, 456, "flood", "3 одинаковых сообщения за 10с"
        )

    async def test_extreme_spam_applies_ban(self):
        message = make_message()
        guard = AsyncMock()
        guard.record.return_value = (10, True, "ban")
        guard.recent_message_ids.return_value = list(range(1001, 1011))
        bot = SimpleNamespace(delete_messages=AsyncMock())
        punish = AsyncMock()
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value={**flood_settings(), "punishment": "ban"})),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "apply_punishment", punish),
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
        ):
            violated = await moderation.ModerationMiddleware()._violates(message, bot)
        self.assertTrue(violated)
        punish.assert_awaited_once_with(unittest.mock.ANY, -100123, 456, "ban")


    async def test_photo_rating_bait_with_emoji_is_sent_to_ai_and_uses_group_setting(self):
        message = make_message()
        message.text = "Кто может оценить мои фоточки? я не против❤️"
        guard = AsyncMock()
        guard.record.return_value = (1, False, None)
        bot = SimpleNamespace(delete_messages=AsyncMock())
        settings = flood_settings()
        settings["punishment"] = "ban"
        ai_result = moderation.ai_guard.AIResult("malicious", "spam", 0.98, "Приманка для личных сообщений")
        punish = AsyncMock()
        ai = AsyncMock(return_value=ai_result)
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=settings)),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "apply_punishment", punish),
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
            patch.object(moderation.ai_guard, "analyze_message", ai),
            patch.object(moderation.ai_guard, "record_violation", AsyncMock(return_value=(1, "mute"))),
        ):
            violated = await moderation.ModerationMiddleware()._violates(message, bot)

        self.assertTrue(violated)
        ai.assert_awaited_once()
        message.delete.assert_awaited_once()
        punish.assert_awaited_once_with(unittest.mock.ANY, -100123, 456, "ban")

    async def test_unicode_flood_is_banned_even_if_message_delete_fails(self):
        message = make_message()
        message.text = ("𒐫" * 12 + " ") * 20
        message.delete.side_effect = TelegramForbiddenError(
            method=SimpleNamespace(), message="delete permission missing"
        )
        guard = AsyncMock()
        guard.record.return_value = (1, False, None)
        bot = SimpleNamespace(delete_messages=AsyncMock())
        punish = AsyncMock()
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value={**flood_settings(), "punishment": "ban"})),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "apply_punishment", punish),
            patch.object(moderation, "write_log", AsyncMock()),
            patch.object(moderation, "notify_moderation", AsyncMock()),
            patch.object(moderation.ai_guard, "analyze_message", AsyncMock()),
        ):
            violated = await moderation.ModerationMiddleware()._violates(message, bot)

        self.assertTrue(violated)
        punish.assert_awaited_once_with(unittest.mock.ANY, -100123, 456, "ban")

    async def test_unicode_glyph_flood_is_deleted_and_banned_immediately(self):
        message = make_message()
        message.text = ("𒐫" * 12 + " ") * 20
        guard = AsyncMock()
        guard.record.return_value = (1, False, None)
        bot = SimpleNamespace(delete_messages=AsyncMock())
        punish = AsyncMock()
        write_log = AsyncMock()
        with (
            patch.object(moderation, "local_flood_guard", guard),
            patch.object(moderation, "get_settings", AsyncMock(return_value=flood_settings())),
            patch.object(moderation, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "apply_punishment", punish),
            patch.object(moderation, "write_log", write_log),
            patch.object(moderation, "notify_moderation", AsyncMock()),
            patch.object(moderation.ai_guard, "analyze_message", AsyncMock()) as ai,
        ):
            violated = await moderation.ModerationMiddleware()._violates(message, bot)

        self.assertTrue(violated)
        message.delete.assert_awaited_once()
        punish.assert_awaited_once_with(unittest.mock.ANY, -100123, 456, "ban")
        write_log.assert_any_await(
            -100123, 456, "unicode_flood", unittest.mock.ANY
        )
        ai.assert_not_awaited()

    async def test_unicode_glyph_filter_does_not_ban_repeated_letters_or_short_symbols(self):
        from textfilters import is_unicode_glyph_flood

        base = ("𒐫" * 12 + " ") * 20
        self.assertFalse(is_unicode_glyph_flood("A" * 500))
        self.assertFalse(is_unicode_glyph_flood("𒐫" * 179))
        self.assertTrue(is_unicode_glyph_flood(base))
        # Appended symbols/text must not dilute the repeated-glyph count.
        self.assertTrue(is_unicode_glyph_flood(base + "!?abcXYZ" * 40))
        self.assertTrue(is_unicode_glyph_flood(base + " обычный текст в конце " * 20))
        mixed_symbols = "¤§¶※" * 45
        self.assertTrue(is_unicode_glyph_flood(mixed_symbols))
        self.assertFalse(is_unicode_glyph_flood("обычный текст с эмодзи ❤️ " * 10))

    async def test_five_consecutive_messages_from_same_member_trigger_burst_detection(self):
        guard = LocalFloodGuard()
        counts = []
        for message_id in range(100, 105):
            counts.append(await guard.record_consecutive_message(-1001, 42, message_id))
        self.assertEqual(counts, [1, 2, 3, 4, 5])
        self.assertEqual(await guard.consecutive_message_ids(-1001, 42), [100, 101, 102, 103, 104])
        self.assertEqual(await guard.record_consecutive_message(-1001, 99, 105), 1)

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
        guard.record.return_value = (6, True, "mute")
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
        guard.record.return_value = (1, False, None)
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
