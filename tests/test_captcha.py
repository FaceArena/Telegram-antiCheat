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
    async def test_unrestrict_reads_flat_chat_member_restricted_permissions(self):
        member = SimpleNamespace(
            status=ChatMemberStatus.RESTRICTED,
            **{field: False for field in captcha._FALLBACK_PERMS},
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=member),
            get_chat=AsyncMock(return_value=SimpleNamespace(permissions=None)),
            restrict_chat_member=AsyncMock(),
        )

        restored = await captcha.unrestrict(bot, -1001, 42)

        self.assertTrue(restored)
        bot.restrict_chat_member.assert_awaited_once()
        self.assertTrue(all(
            getattr(bot.restrict_chat_member.await_args.kwargs["permissions"], field) is True
            for field in captcha._FALLBACK_PERMS
        ))

    async def test_unrestrict_accepts_omitted_optional_permission_flags(self):
        member = SimpleNamespace(
            status=ChatMemberStatus.RESTRICTED,
            can_send_messages=False,
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=member),
            get_chat=AsyncMock(return_value=SimpleNamespace(permissions=None)),
            restrict_chat_member=AsyncMock(),
        )

        restored = await captcha.unrestrict(bot, -1001, 42)

        self.assertTrue(restored)
        bot.restrict_chat_member.assert_awaited_once()

    async def test_unrestrict_keeps_admin_changed_restricted_permissions(self):
        member = SimpleNamespace(
            status=ChatMemberStatus.RESTRICTED,
            **{field: False for field in captcha._FALLBACK_PERMS},
        )
        member.can_send_messages = True
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=member),
            get_chat=AsyncMock(),
            restrict_chat_member=AsyncMock(),
        )

        restored = await captcha.unrestrict(bot, -1001, 42)

        self.assertFalse(restored)
        bot.get_chat.assert_not_awaited()
        bot.restrict_chat_member.assert_not_awaited()

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
                edit_text=AsyncMock(),
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

    async def test_timeout_worker_never_bans_or_unbans_after_captcha_timeout(self):
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
        bot.ban_chat_member.assert_not_awaited()
        bot.unban_chat_member.assert_not_awaited()
        bot.delete_message.assert_awaited_once_with(-1001, 123)

    async def test_timeout_worker_does_not_unban_manual_admin_ban(self):
        pool = SimpleNamespace(
            fetch=AsyncMock(return_value=[{
                "chat_id": -1001,
                "user_id": 42,
                "message_id": 123,
                "attempts": 1,
                "kick_started": True,
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


    async def test_callback_acknowledges_before_database_lookup(self):
        call = SimpleNamespace(
            data="c:-1001:42:current-token",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                edit_text=AsyncMock(),
                chat=SimpleNamespace(id=-1001),
                message_id=123,
            ),
            answer=AsyncMock(),
        )

        async def fetchrow(*args, **kwargs):
            call.answer.assert_awaited_once_with("Проверяем…")
            return None

        pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=fetchrow))
        with patch.object(captcha.common, "pool", pool, create=True):
            await captcha.on_captcha_click(call, SimpleNamespace())

        call.answer.assert_awaited_once_with("Проверяем…")
        call.message.edit_text.assert_awaited_once_with(
            "Время вышло, проверка уже выполняется или завершена.",
            reply_markup=None,
        )

    async def test_callback_rejects_stale_challenge_token(self):
        pool = SimpleNamespace(
            fetchrow=AsyncMock(return_value={
                "message_id": 0,
                "challenge_token": "current-token",
            }),
            execute=AsyncMock(),
        )
        call = SimpleNamespace(
            data="c:-1001:42:old-token",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                edit_text=AsyncMock(),
                chat=SimpleNamespace(id=-1001),
                message_id=123,
            ),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_chat=AsyncMock(),
            restrict_chat_member=AsyncMock(),
        )

        with patch.object(captcha.common, "pool", pool, create=True):
            await captcha.on_captcha_click(call, bot)

        call.answer.assert_awaited_once_with("Проверяем…")
        call.message.edit_text.assert_awaited_once_with(
            "Эта кнопка уже недействительна.",
            reply_markup=None,
        )
        bot.get_chat.assert_not_awaited()
        bot.restrict_chat_member.assert_not_awaited()

    async def test_callback_accepts_current_token_before_message_id_is_saved(self):
        pool = SimpleNamespace(
            fetchrow=AsyncMock(return_value={
                "message_id": 0,
                "challenge_token": "current-token",
            }),
            execute=AsyncMock(),
        )
        call = SimpleNamespace(
            data="c:-1001:42:current-token",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                edit_text=AsyncMock(),
                chat=SimpleNamespace(id=-1001),
                message_id=123,
            ),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(
                status=ChatMemberStatus.RESTRICTED,
                **{field: False for field in captcha._FALLBACK_PERMS},
            )),
            get_chat=AsyncMock(return_value=SimpleNamespace(permissions=None)),
            restrict_chat_member=AsyncMock(),
            delete_message=AsyncMock(),
        )

        with patch.object(captcha.common, "pool", pool, create=True):
            await captcha.on_captcha_click(call, bot)

        bot.restrict_chat_member.assert_awaited_once()
        bot.delete_message.assert_awaited_once_with(-1001, 123)
        call.answer.assert_awaited_once_with("Проверяем…")

    async def test_callback_does_not_restrict_user_again_when_pending_delete_fails(self):
        pool = SimpleNamespace(
            fetchrow=AsyncMock(return_value={
                "message_id": 123,
                "challenge_token": "current-token",
            }),
            execute=AsyncMock(side_effect=[
                RuntimeError("database temporarily unavailable"),
                "UPDATE 1",
            ]),
            fetchval=AsyncMock(return_value=True),
        )
        call = SimpleNamespace(
            data="c:-1001:42:current-token",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                edit_text=AsyncMock(),
                chat=SimpleNamespace(id=-1001),
                message_id=123,
            ),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(
                status=ChatMemberStatus.MEMBER,
            )),
            get_chat=AsyncMock(),
            restrict_chat_member=AsyncMock(),
            delete_message=AsyncMock(),
        )

        with patch.object(captcha.common, "pool", pool, create=True):
            await captcha.on_captcha_click(call, bot)

        bot.restrict_chat_member.assert_not_awaited()
        bot.get_chat.assert_not_awaited()
        self.assertEqual(pool.execute.await_count, 2)
        self.assertIn("processing_until = NULL", pool.execute.await_args_list[1].args[0])
        call.answer.assert_awaited_once_with("Проверяем…")
        call.message.edit_text.assert_awaited_once_with(
            "Проверка пройдена, доступ восстановлен. Запись будет очищена повторно.",
            reply_markup=None,
        )

    async def test_callback_does_not_overwrite_permissions_changed_by_admin(self):
        pool = SimpleNamespace(
            fetchrow=AsyncMock(return_value={
                "message_id": 123,
                "challenge_token": "current-token",
            }),
            execute=AsyncMock(),
        )
        call = SimpleNamespace(
            data="c:-1001:42:current-token",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                edit_text=AsyncMock(),
                chat=SimpleNamespace(id=-1001),
                message_id=123,
            ),
            answer=AsyncMock(),
        )
        changed_permissions = captcha.captcha_restricted_permissions()
        changed_permissions.can_send_messages = True
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(
                status=ChatMemberStatus.RESTRICTED,
                permissions=changed_permissions,
            )),
            get_chat=AsyncMock(),
            restrict_chat_member=AsyncMock(),
            delete_message=AsyncMock(),
        )

        with patch.object(captcha.common, "pool", pool, create=True):
            await captcha.on_captcha_click(call, bot)

        bot.get_chat.assert_not_awaited()
        bot.restrict_chat_member.assert_not_awaited()
        pool.execute.assert_awaited_once()
        self.assertIn("processing_until = NULL", pool.execute.await_args.args[0])
        call.answer.assert_awaited_once_with("Проверяем…")
        call.message.edit_text.assert_awaited_once_with(
            "Права изменились во время проверки. Администратор должен проверить участника.",
            reply_markup=None,
        )

    async def test_callback_rejects_button_from_another_chat(self):
        call = SimpleNamespace(
            data="c:-1001:42",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                edit_text=AsyncMock(),
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
