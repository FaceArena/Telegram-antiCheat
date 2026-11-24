import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiogram.enums import ChatType
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message

from autodelete import AutoDeleteMiddleware


def sent_message(chat_type, message_id):
    chat_id = 42 if chat_type == ChatType.PRIVATE else -1001
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        text="test",
    )


class AutoDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedules_group_message_without_keyboard(self):
        result = sent_message(ChatType.SUPERGROUP, 77)
        bot = type("BotStub", (), {"delete_message": AsyncMock()})()
        middleware = AutoDeleteMiddleware(delay=0)

        async def request(_bot, _method):
            return result

        returned = await middleware(request, bot, SendMessage(chat_id=-1001, text="notice"))
        self.assertIs(returned, result)
        await asyncio.sleep(0)
        bot.delete_message.assert_awaited_once_with(-1001, 77)

    async def test_keeps_messages_with_inline_keyboard(self):
        result = sent_message(ChatType.SUPERGROUP, 78)
        bot = type("BotStub", (), {"delete_message": AsyncMock()})()
        middleware = AutoDeleteMiddleware(delay=0)
        method = SendMessage(
            chat_id=-1001, text="captcha",
            reply_markup={"inline_keyboard": []},
        )

        async def request(_bot, _method):
            return result

        await middleware(request, bot, method)
        await asyncio.sleep(0)
        bot.delete_message.assert_not_awaited()

    async def test_keeps_private_messages(self):
        result = sent_message(ChatType.PRIVATE, 79)
        bot = type("BotStub", (), {"delete_message": AsyncMock()})()
        middleware = AutoDeleteMiddleware(delay=0)

        async def request(_bot, _method):
            return result

        await middleware(request, bot, SendMessage(chat_id=42, text="private"))
        await asyncio.sleep(0)
        bot.delete_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
