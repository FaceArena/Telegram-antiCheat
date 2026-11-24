"""Delete ordinary bot messages in groups after a short delay.

Messages with inline keyboards are deliberately excluded: CAPTCHA and other
interactive controls must remain usable. Private-chat messages are retained.
"""
import asyncio
import logging

from aiogram.enums import ChatType
from aiogram.methods import SendMessage
from aiogram.types import Message


DELETE_AFTER_SECONDS = 4


class AutoDeleteMiddleware:
    def __init__(self, delay: int = DELETE_AFTER_SECONDS):
        self.delay = delay
        self._tasks: set[asyncio.Task] = set()

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        if (
            isinstance(method, SendMessage)
            and isinstance(result, Message)
            and result.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
            and method.reply_markup is None
        ):
            task = asyncio.create_task(
                self._delete_later(bot, result.chat.id, result.message_id),
                name=f"auto-delete-{result.chat.id}-{result.message_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return result

    async def _delete_later(self, bot, chat_id: int, message_id: int):
        try:
            await asyncio.sleep(self.delay)
            await bot.delete_message(chat_id, message_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.debug(
                "Не удалось автоматически удалить сообщение бота "
                "(chat_id=%s, message_id=%s)",
                chat_id,
                message_id,
                exc_info=True,
            )
