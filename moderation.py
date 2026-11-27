import asyncio
import logging

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

import common
import ai_guard
from common import is_chat_admin
from protection import (
    get_settings,
    apply_punishment,
    notify_moderation,
    write_log,
)
from flood import local_flood_guard, MAX_FLOOD_LIMIT, MAX_FLOOD_WINDOW
from textfilters import find_stopword, normalize_text, is_unicode_glyph_flood, is_photo_rating_bait

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

        message_text = message.text or message.caption or ""
        if reason is None and message_text and is_photo_rating_bait(message_text):
            reason = "photo_rating_bait"
            details = "просьба оценить личные фотографии"

        if reason is None and message_text and is_unicode_glyph_flood(message_text):
            reason = "unicode_flood"
            details = f"массовый повтор Unicode-символа; длина={len(message_text)}"

        if reason is None and message_text and is_photo_rating_bait(message_text):
            reason = "photo_rating_bait"
            details = "поиск оценщиков личных фотографий"

        if reason is None and message_text:
            entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []
            has_link = any(getattr(entity.type, "value", entity.type) in {"url", "text_link"} for entity in entities)
            try:
                ai_result = await ai_guard.analyze_message(message_text, has_link=has_link)
            except Exception:
                logging.exception("Ошибка AI-анализа сообщения в чате %s", chat_id)
                ai_result = None
            if ai_result and ai_result.risk == "malicious":
                enforceable = ai_result.category in {"phishing", "scam", "impersonation", "malware", "malicious_link"} or (ai_result.category == "spam" and ai_result.confidence >= 0.97)
                if enforceable and ai_result.confidence >= 0.92:
                    reason = "ai_guard"
                    details = f"category={ai_result.category}; confidence={ai_result.confidence:.2f}; reason={ai_result.reason}"
                elif ai_result.confidence >= 0.80:
                    await write_log(chat_id, user.id, "ai_guard_review", f"category={ai_result.category}; confidence={ai_result.confidence:.2f}; reason={ai_result.reason}")
            elif ai_result and ai_result.risk == "suspicious" and ai_result.confidence >= 0.80:
                await write_log(chat_id, user.id, "ai_guard_review", f"category={ai_result.category}; confidence={ai_result.confidence:.2f}; reason={ai_result.reason}")

        if reason is None and settings["stopwords"] and message_text:
            word = find_stopword(message_text, tuple(settings["stopword_list"]))
            if word:
                reason, details = "stopword", word

        if reason is None:
            return False

        if reason == "ai_guard":
            try:
                incident_count, _escalation = await ai_guard.record_violation(
                    common.pool,
                    chat_id,
                    user.id,
                    message.message_id,
                    ai_result.category,
                    ai_result.confidence,
                )
            except Exception:
                # If persistence is unavailable, still remove a high-confidence
                # malicious message, but do not escalate to mute or ban.
                logging.exception("Не удалось сохранить AI-инцидент; применяем только удаление")
                incident_count = 0
            delete_succeeded = True
            try:
                await message.delete()
            except TelegramAPIError as exc:
                delete_succeeded = False
                logging.warning("AI обнаружил вредоносное сообщение, но удаление не удалось в %s: %s", chat_id, exc)
                await write_log(chat_id, user.id, "ai_guard_delete_failed", f"message_id={message.message_id}; {details or ''}")
            # A failed message deletion must never silently change the configured
            # punishment. The selected action remains authoritative.
            await write_log(chat_id, user.id, "ai_guard_violation", f"message_id={message.message_id}; incidents_24h={incident_count}; action={punishment}; {details or ''}")
            punishment = settings["punishment"]
            punishment_succeeded = punishment == "delete"
            if punishment != "delete":
                try:
                    await apply_punishment(bot, chat_id, user.id, punishment)
                    punishment_succeeded = True
                except TelegramAPIError as exc:
                    logging.warning("AI-наказание %s не удалось применить к %s в %s: %s", punishment, user.id, chat_id, exc)
                    await write_log(chat_id, user.id, "ai_guard_punishment_failed", f"message_id={message.message_id}; action={punishment}")
            if punishment_succeeded:
                await notify_moderation(bot, chat_id, f"@{user.username}" if user.username else user.full_name, punishment, f"AI: {ai_result.category}", f"уверенность {ai_result.confidence:.0%}; нарушений за 24 ч: {incident_count}")
            return True

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
                # Message deletion and user restriction are separate Telegram
                # permissions. A failed delete must not cancel the punishment.
                logging.warning(
                    "Не удалось удалить нарушающее сообщение в %s; продолжаем наказание: %s",
                    chat_id,
                    exc,
                )
                await write_log(
                    chat_id, user.id, f"{reason}_delete_failed",
                    f"message_id={message.message_id}; {details or ''}",
                )

        # For flood, retry the configured punishment on every over-limit message.
        # A single transient Telegram API failure on the threshold message must not
        # permanently downgrade the protection to message deletion only.
        # The log records every message that the bot actually removed, including
        # later messages arriving while the same user is already muted.
        await write_log(chat_id, user.id, reason, details or "")
        should_attempt_punishment = (reason == "flood" and flood_action is not None) or first_violation or reason in {"stopword", "unicode_flood", "photo_rating_bait"}
        if should_attempt_punishment:
            # Every detector uses the same group-level punishment setting.
            # flood_action only marks a new flood incident; it does not choose
            # the punishment or escalate it. 
            punishment = settings["punishment"]
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
            if first_violation or flood_action or reason in {"stopword", "unicode_flood", "photo_rating_bait"}:
                if punishment_succeeded:
                    await notify_moderation(
                        bot,
                        chat_id,
                        f"@{user.username}" if user.username else user.full_name,
                        punishment,
                        {
                            "flood": "флуд",
                            "stopword": "запрещённая фраза",
                            "unicode_flood": "массовый повтор Unicode-символа",
                            "photo_rating_bait": "спам с просьбой оценить фотографии",
                        }.get(reason, reason),
                        details or "",
                    )

        return True

