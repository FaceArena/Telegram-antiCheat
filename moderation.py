import asyncio
import logging

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

import common
from common import is_chat_admin
from protection import (
    get_settings,
    apply_punishment,
    notify_moderation,
    write_log,
)
from flood import local_flood_guard, MAX_FLOOD_LIMIT, MAX_FLOOD_WINDOW
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
        try:
            settings = await get_settings(chat_id)
        except Exception:
            logging.exception(
                "Не удалось загрузить настройки группы %s; включены безопасные значения антифлуда",
                chat_id,
            )
            settings = None
        if not settings:
            # The baseline anti-flood must still protect a group if its settings
            # row is missing or PostgreSQL is temporarily unavailable.
            settings = {
                "antiflood": False,
                "stopwords": False,
                "flood_limit": 5,
                "flood_window": 5,
                "stopword_list": [],
                "punishment": "delete",
            }
        try:
            if await is_chat_admin(bot, chat_id, user.id):
                return False
        except TelegramAPIError:
            # Fallback to a direct per-member check if the administrator-list
            # request failed. Never punish when Telegram cannot confirm status.
            try:
                from aiogram.enums import ChatMemberStatus
                member = await bot.get_chat_member(chat_id, user.id)
                if member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
                    return False
            except TelegramAPIError:
                logging.warning(
                    "Не удалось проверить статус автора в %s; наказание пропущено",
                    chat_id,
                    exc_info=True,
                )
                return False

        reason = details = None
        first_violation = False

        flood_action = None
        if self.check_flood:
            # Hard baseline: 5 messages / 5 seconds, regardless of the UI
            # toggle. Group settings may make the limit stricter, never weaker.
            limit = min(MAX_FLOOD_LIMIT, int(settings["flood_limit"]))
            window = min(MAX_FLOOD_WINDOW, int(settings["flood_window"]))
            message_text = message.text or message.caption or ""
            fingerprint = normalize_text(message_text) if message_text.strip() else None
            count, exceeded, flood_action = await local_flood_guard.record(
                chat_id=chat_id,
                user_id=user.id,
                message_id=message.message_id,
                media_group_id=message.media_group_id,
                limit=limit,
                window=window,
                fingerprint=fingerprint,
            )
            logging.debug(
                "Локальный антифлуд: chat_id=%s user_id=%s count=%s limit=%s window=%ss action=%s",
                chat_id, user.id, count, limit, window, flood_action,
            )
            if exceeded:
                reason = "flood"
                first_violation = count == limit + 1
                details = (
                    f"более {limit} сообщений за {window}с"
                    if count > limit
                    else "3 одинаковых сообщения за 10с"
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

        if reason == "flood":
            # Telegram delivers album parts as separate updates. Give the
            # remaining parts a short collection window before deleting them.
            await asyncio.sleep(0.35)
            # Once flood is detected, remove the whole burst from this user,
            # not just the latest over-limit message.
            message_ids = await local_flood_guard.recent_message_ids(chat_id, user.id)
            if message_ids:
                try:
                    await bot.delete_messages(chat_id, message_ids[:100])
                except TelegramAPIError as exc:
                    logging.warning(
                        "Не удалось удалить пачку сообщений флуда в %s: %s",
                        chat_id,
                        exc,
                    )
                    # Fallback to per-message deletion so one unavailable item
                    # does not prevent cleanup of the remaining burst.
                    for message_id in message_ids[:100]:
                        try:
                            await bot.delete_message(chat_id, message_id)
                        except TelegramAPIError:
                            logging.debug(
                                "Не удалось удалить сообщение %s из пачки флуда в %s",
                                message_id,
                                chat_id,
                                exc_info=True,
                            )
        else:
            try:
                await message.delete()
            except TelegramAPIError as exc:
                logging.warning(
                    "Нет прав удалить сообщение в %s: %s",
                    chat_id,
                    exc,
                )
                return False

        # For flood, retry the configured punishment on every over-limit message.
        # A single transient Telegram API failure on the threshold message must not
        # permanently downgrade the protection to message deletion only.
        # The log records every message that the bot actually removed, including
        # later messages arriving while the same user is already muted.
        await write_log(chat_id, user.id, reason, details or "")
        should_attempt_punishment = (reason == "flood" and flood_action is not None) or first_violation or reason == "stopword"
        if should_attempt_punishment:
            punishment = flood_action if reason == "flood" and flood_action else settings["punishment"]
            punishment_succeeded = punishment == "delete"
            try:
                await apply_punishment(
                    bot,
                    chat_id,
                    user.id,
                    punishment,
                )
                punishment_succeeded = True
            except TelegramAPIError as exc:
                if reason == "flood" and flood_action:
                    await local_flood_guard.punishment_failed(chat_id, user.id, flood_action)
                logging.warning(
                    "Не удалось наказать %s в %s (причина=%s, попытка=%s): %s",
                    user.id,
                    chat_id,
                    reason,
                    "порог" if first_violation else "повтор",
                    exc,
                )

            # One normal notice per incident; failures are logged so an operator
            # can distinguish a missing permission from a filter that did not fire.
            if first_violation or flood_action or reason == "stopword":
                if punishment_succeeded:
                    await notify_moderation(
                        bot,
                        chat_id,
                        f"@{user.username}" if user.username else user.full_name,
                        punishment,
                        {
                            "flood": "флуд",
                            "stopword": "запрещённая фраза",
                        }.get(reason, reason),
                        details or "",
                    )

        return True

