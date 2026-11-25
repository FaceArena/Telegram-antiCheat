import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.types import Chat, Message, Update, User

import bot as bot_app
import captcha
import protection
from moderation import ModerationMiddleware


class DispatcherModerationWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")

    async def asyncTearDown(self):
        await self.bot.session.close()

    def make_message(self, message_id=1):
        return Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-100123, type=ChatType.SUPERGROUP, title="Test"),
            from_user=User(id=456, is_bot=False, first_name="Member"),
            text="hello",
        )

    async def test_dispatcher_level_middleware_runs_without_router_local_handlers(self):
        dp = Dispatcher()
        router = Router()
        handled = []

        @router.message()
        async def receive_message(message: Message):
            handled.append(message.message_id)

        middleware = ModerationMiddleware()
        check = AsyncMock(return_value=False)
        middleware._violates = check
        dp.message.outer_middleware(middleware)
        dp.include_router(router)

        await dp.feed_update(
            self.bot,
            Update(update_id=1, message=self.make_message()),
        )

        check.assert_awaited_once()
        self.assertEqual(handled, [1])

    async def test_dispatcher_level_middleware_runs_for_edited_messages(self):
        dp = Dispatcher()
        router = Router()
        handled = []

        @router.edited_message()
        async def receive_edit(message: Message):
            handled.append(message.message_id)

        middleware = ModerationMiddleware(check_flood=False)
        check = AsyncMock(return_value=False)
        middleware._violates = check
        dp.edited_message.outer_middleware(middleware)
        dp.include_router(router)

        await dp.feed_update(
            self.bot,
            Update(update_id=2, edited_message=self.make_message(2)),
        )

        check.assert_awaited_once()
        self.assertEqual(handled, [2])


class ChatMemberPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_handler_invokes_captcha_pipeline_before_early_return(self):
        target = User(id=42, is_bot=False, first_name="Member")
        event = type("Event", (), {
            "chat": Chat(id=-100123, type=ChatType.SUPERGROUP, title="Test"),
            "old_chat_member": type("Old", (), {"status": "member"})(),
            "new_chat_member": type("New", (), {"status": "member", "user": target})(),
            "from_user": target,
        })()

        with (
            patch.object(bot_app, "invalidate_admin_cache", new=AsyncMock()),
            patch.object(captcha, "on_member_update", new=AsyncMock()) as captcha_handler,
            patch.object(protection, "write_log", new=AsyncMock()),
        ):
            await bot_app.audit_member_status_change(event, self._bot_stub())

        captcha_handler.assert_awaited_once_with(event, unittest.mock.ANY)

    @staticmethod
    def _bot_stub():
        return object()


class EditedProtectionRegistrationTests(unittest.TestCase):
    def test_link_protection_has_an_edited_message_handler(self):
        self.assertTrue(protection.router.edited_message.handlers)


if __name__ == "__main__":
    unittest.main()
