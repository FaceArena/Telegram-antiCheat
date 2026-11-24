import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import ChatPermissions

import captcha


class CaptchaPermissionTests(unittest.TestCase):
    def test_captcha_disables_every_send_permission(self):
        permissions = captcha.captcha_restricted_permissions()

        for field in captcha._FALLBACK_PERMS:
            with self.subTest(field=field):
                self.assertIs(getattr(permissions, field), False)

    def test_unrestrict_fallback_enables_every_send_permission(self):
        permissions = ChatPermissions(
            **{field: True for field in captcha._FALLBACK_PERMS}
        )

        for field in captcha._FALLBACK_PERMS:
            with self.subTest(field=field):
                self.assertIs(getattr(permissions, field), True)


class CaptchaAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_delete_skips_placeholder_message_id(self):
        bot = SimpleNamespace(delete_message=AsyncMock())

        await captcha.safe_delete(bot, -1001, 0)

        bot.delete_message.assert_not_awaited()

    async def test_safe_delete_swallows_telegram_delete_error(self):
        bot = SimpleNamespace(
            delete_message=AsyncMock(
                side_effect=TelegramForbiddenError(
                    method=SimpleNamespace(),
                    message="not enough rights",
                )
            )
        )

        await captcha.safe_delete(bot, -1001, 123)

        bot.delete_message.assert_awaited_once_with(-1001, 123)

    async def test_callback_rejects_another_user_before_database_access(self):
        call = SimpleNamespace(
            data="c:-1001:42",
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-1001),
                message_id=123,
            ),
            answer=AsyncMock(),
        )

        with patch.object(captcha.common, "pool", create=True) as pool:
            await captcha.on_captcha_click(call, SimpleNamespace())

        call.answer.assert_awaited_once_with(
            "Эта кнопка не для вас.",
            show_alert=True,
        )
        pool.acquire.assert_not_called()

    async def test_timeout_worker_does_not_hold_database_connection_for_kick(self):
        pool = SimpleNamespace(
            fetch=AsyncMock(return_value=[{
                "chat_id": -1001,
                "user_id": 42,
                "message_id": 123,
                "attempts": 1,
                "kick_started": False,
            }]),
            execute=AsyncMock(),
            acquire=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status=ChatMemberStatus.MEMBER)
            ),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            delete_message=AsyncMock(),
        )

        with (
            patch.object(captcha.common, "pool", pool, create=True),
            patch("captcha.asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await captcha.captcha_worker(bot)

        pool.acquire.assert_not_awaited()
        bot.ban_chat_member.assert_awaited_once_with(-1001, 42)
        bot.unban_chat_member.assert_awaited_once()
        bot.delete_message.assert_awaited_once_with(-1001, 123)

    async def test_timeout_worker_does_not_unban_manual_admin_ban(self):
        pool = SimpleNamespace(
            fetch=AsyncMock(return_value=[{
                "chat_id": -1001,
                "user_id": 42,
                "message_id": 123,
                "attempts": 1,
                "kick_started": False,
            }]),
            execute=AsyncMock(),
            acquire=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status=ChatMemberStatus.KICKED)
            ),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            delete_message=AsyncMock(),
        )

        with (
            patch.object(captcha.common, "pool", pool, create=True),
            patch("captcha.asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await captcha.captcha_worker(bot)

        bot.unban_chat_member.assert_not_awaited()
        bot.ban_chat_member.assert_not_awaited()
        bot.delete_message.assert_awaited_once_with(-1001, 123)

    async def test_callback_rejects_button_from_another_chat(self):
        call = SimpleNamespace(
            data="c:-1001:42",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-1002),
                message_id=123,
            ),
            answer=AsyncMock(),
        )

        with patch.object(captcha.common, "pool", create=True) as pool:
            await captcha.on_captcha_click(call, SimpleNamespace())

        call.answer.assert_awaited_once_with(
            "Кнопка недействительна.",
            show_alert=True,
        )
        pool.acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
