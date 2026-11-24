import logging

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

import common
from protection import (
    apply_punishment,
    notify_moderation,
    write_log,
)
from flood import record_flood_message
from textfilters import find_stopword, normalize_text

router = Router()
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
MAX_WORDS = 200
MAX_REPLY_LENGTH = 3500


# ---------- Проверка каждого сообщения ----------
class ModerationMiddleware(BaseMiddleware):
    def __init__(self, check_flood: bool = True):
        self.check_flood = check_flood  # правки сообщений на флуд не проверяем

    async def __call__(self, handler, event: Message, data):
        if await self._violates(event, data["bot"]):
            return None  # нарушитель: дальше сообщение не идёт
        return await handler(event, data)

    async def _violates(self, message: Message, bot: Bot) -> bool:
        if message.chat.type not in GROUPS:
            return False
        if message.sender_chat or message.is_automatic_forward:
            return False
        if message.new_chat_members or message.left_chat_member:
            return False

        user = message.from_user
        if user is None or user.is_bot:
            return False

        chat_id = message.chat.id
        settings = await get_settings(chat_id)
        if not settings or not (settings["antiflood"] or settings["stopwords"]):
            return False
        try:
            if await is_chat_admin(bot, chat_id, user.id):
                return False
        except TelegramAPIError:
            # Не наказываем участника, если Telegram не подтвердил его статус.
            logging.warning(
                "Не удалось проверить права автора сообщения в %s; модерацию пропускаем",
                chat_id,
                exc_info=True,
            )
            return False

        reason = details = None
        log_and_punish = True

        if self.check_flood and settings["antiflood"]:
            try:
                count = await record_flood_message(
                    common.pool,
                    chat_id=chat_id,
                    user_id=user.id,
                    message_id=message.message_id,
                    media_group_id=message.media_group_id,
                    window=settings["flood_window"],
                )
            except Exception:
                # При недоступности БД не применяем наказание на основании
                # неполного счётчика; остальные фильтры продолжают работать.
                logging.exception(
                    "Не удалось зарегистрировать сообщение для антифлуда "
                    "(chat_id=%s, user_id=%s)",
                    chat_id,
                    user.id,
                )
            else:
                if count > settings["flood_limit"]:
                    reason = "flood"
                    log_and_punish = count == settings["flood_limit"] + 1
                    details = (
                        f"{settings['flood_limit']}+ сообщений "
                        f"за {settings['flood_window']}с"
                    )

        if reason is None and settings["stopwords"]:
            message_text = message.text or message.caption
            if message_text:
                word = find_stopword(
                    message_text,
                    tuple(settings["stopword_list"]),
                )
                if word:
                    reason, details = "stopword", word

        if reason is None:
            return False

        try:
            await message.delete()
        except TelegramAPIError as exc:
            logging.warning(
                "Нет прав удалить сообщение в %s: %s",
                chat_id,
                exc,
            )
            return False

        if log_and_punish:
            punishment_succeeded = settings["punishment"] == "delete"
            try:
                await apply_punishment(
                    bot,
                    chat_id,
                    user.id,
                    settings["punishment"],
                )
                punishment_succeeded = True
            except TelegramAPIError as exc:
                logging.warning(
                    "Не удалось наказать %s в %s: %s",
                    user.id,
                    chat_id,
                    exc,
                )

            await write_log(chat_id, user.id, reason, details or "")

            if punishment_succeeded:
                await notify_moderation(
                    bot,
                    chat_id,
                    f"@{user.username}" if user.username else user.full_name,
                    settings["punishment"],
                    {
                        "flood": "флуд",
                        "stopword": "запрещённая фраза",
                    }.get(reason, reason),
                    details or "",
                )

        return True


router.message.outer_middleware(ModerationMiddleware(check_flood=True))
router.edited_message.outer_middleware(ModerationMiddleware(check_flood=False))
