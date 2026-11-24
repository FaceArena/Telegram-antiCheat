"""Manual cleanup of bot accounts observed joining connected groups.

Telegram Bot API cannot enumerate all group members. This module therefore
only cleans bot accounts captured from chat_member updates after installation.
"""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup

import common

logger = logging.getLogger(__name__)
router = Router()
_pool = None
BATCH_SIZE = 100
OBSERVATION_DAYS = 90


def configure(pool) -> None:
    global _pool
    _pool = pool


def is_new_bot_join(old_status, new_status, is_bot: bool) -> bool:
    """Return true only for a bot newly joining a group, not status edits."""
    if not is_bot:
        return False
    was_out = old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    is_in = new_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.ADMINISTRATOR,
    )
    return was_out and is_in


async def _authorized_group(call: CallbackQuery, bot: Bot, chat_id: int) -> bool:
    if _pool is None:
        await call.answer("Сервис очистки пока не готов.", show_alert=True)
        return False
    try:
        is_admin = await common.is_chat_admin(bot, chat_id, call.from_user.id, fresh=True)
    except TelegramAPIError:
        await call.answer("Не удалось проверить права администратора. Повторите позже.", show_alert=True)
        return False
    if not is_admin:
        await call.answer("Управлять очисткой могут только администраторы группы.", show_alert=True)
        return False
    chat = await _pool.fetchrow(
        """
        SELECT 1 FROM chats
        WHERE chat_id = $1 AND active
          AND chat_type IN ('group', 'supergroup')
        """,
        chat_id,
    )
    if not chat:
        await call.answer("Группа не найдена или отключена.", show_alert=True)
        return False
    return True


@router.chat_member(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.new_chat_member.user.is_bot)
async def remember_joined_bot(event: ChatMemberUpdated):
    """Persist bot accounts as Telegram reports their membership transitions."""
    if _pool is None:
        return
    member = event.new_chat_member
    if not is_new_bot_join(
        event.old_chat_member.status,
        member.status,
        bool(member.user.is_bot),
    ):
        return
    try:
        await _pool.execute(
            """
            INSERT INTO known_group_bots (
                chat_id, user_id, username, display_name, first_seen_at, last_seen_at, removed_at
            )
            VALUES ($1, $2, $3, $4, now(), now(), NULL)
            ON CONFLICT (chat_id, user_id) DO UPDATE
            SET username = EXCLUDED.username,
                display_name = EXCLUDED.display_name,
                last_seen_at = now(),
                removed_at = NULL
            """,
            event.chat.id,
            member.user.id,
            member.user.username,
            member.user.full_name,
        )
    except Exception:
        logger.exception(
            "Could not remember bot join (chat_id=%s, user_id=%s)",
            event.chat.id,
            member.user.id,
        )


@router.callback_query(F.data.startswith("bc:"))
async def confirm_bot_cleanup(call: CallbackQuery, bot: Bot):
    try:
        chat_id = int((call.data or "").split(":", 1)[1])
    except (TypeError, ValueError):
        return await call.answer("Некорректный запрос.", show_alert=True)
    if not await _authorized_group(call, bot, chat_id):
        return
    count = await _pool.fetchval(
        """
        SELECT COUNT(*) FROM known_group_bots
        WHERE chat_id = $1 AND removed_at IS NULL
          AND last_seen_at >= now() - make_interval(days => $2)
        """,
        chat_id,
        OBSERVATION_DAYS,
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚫 Заблокировать обнаруженных ботов ({count})", callback_data=f"bc_yes:{chat_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"m:{chat_id}")],
    ])
    if call.message:
        await call.message.answer(
            f"Найдено в истории KimGuard: {count} бот-аккаунтов за последние {OBSERVATION_DAYS} дней.\n\n"
            "Будут заблокированы обнаруженные бот-аккаунты, которые сейчас состоят в группе. Бан не позволит им вернуться, пока администратор не снимет блокировку. "
            "Боты-администраторы будут пропущены. Обычные участники не затрагиваются. "
            "Telegram не позволяет просканировать полный список старых участников.",
            reply_markup=keyboard,
        )
    await call.answer()


@router.callback_query(F.data.startswith("bc_yes:"))
async def run_bot_cleanup(call: CallbackQuery, bot: Bot):
    try:
        chat_id = int((call.data or "").split(":", 1)[1])
    except (TypeError, ValueError):
        return await call.answer("Некорректный запрос.", show_alert=True)
    if not await _authorized_group(call, bot, chat_id):
        return
    try:
        me = await bot.me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
    except TelegramAPIError:
        return await call.answer("Не удалось проверить права KimGuard.", show_alert=True)
    if (
        bot_member.status != ChatMemberStatus.ADMINISTRATOR
        or not getattr(bot_member, "can_restrict_members", False)
    ):
        return await call.answer(
            "Для очистки выдайте KimGuard право «Блокировка пользователей».",
            show_alert=True,
        )

    candidates = await _pool.fetch(
        """
        SELECT user_id, username, display_name
        FROM known_group_bots
        WHERE chat_id = $1 AND removed_at IS NULL
          AND last_seen_at >= now() - make_interval(days => $2)
        ORDER BY first_seen_at
        LIMIT $3
        """,
        chat_id,
        OBSERVATION_DAYS,
        BATCH_SIZE,
    )
    removed = 0
    skipped_admins = 0
    already_absent = 0
    failed = 0

    for candidate in candidates:
        user_id = int(candidate["user_id"])
        if user_id == me.id:
            continue
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
                skipped_admins += 1
                await _pool.execute(
                    """
                    INSERT INTO logs (chat_id, user_id, target_user_id, actor_user_id, action, details)
                    VALUES ($1, $2, $2, $3, 'bot_cleanup_skipped_admin', 'Bot administrator was not removed')
                    """,
                    chat_id, user_id, call.from_user.id,
                )
                continue
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                await _pool.execute(
                    "UPDATE known_group_bots SET removed_at = now() WHERE chat_id = $1 AND user_id = $2",
                    chat_id, user_id,
                )
                already_absent += 1
                continue
            try:
                await bot.ban_chat_member(chat_id, user_id)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(max(1, exc.retry_after))
                await bot.ban_chat_member(chat_id, user_id)
            await _pool.execute(
                "UPDATE known_group_bots SET removed_at = now() WHERE chat_id = $1 AND user_id = $2",
                chat_id, user_id,
            )
            await _pool.execute(
                """
                INSERT INTO logs (chat_id, user_id, target_user_id, actor_user_id, action, details)
                VALUES ($1, $2, $2, $3, 'bot_cleanup_banned', $4)
                """,
                chat_id,
                user_id,
                call.from_user.id,
                f"username=@{candidate['username']}" if candidate["username"] else f"name={candidate['display_name']}",
            )
            removed += 1
        except TelegramAPIError:
            failed += 1
            logger.warning(
                "Could not remove known bot (chat_id=%s, user_id=%s)",
                chat_id, user_id, exc_info=True,
            )
            try:
                await _pool.execute(
                    """
                    INSERT INTO logs (chat_id, user_id, target_user_id, actor_user_id, action, details)
                    VALUES ($1, $2, $2, $3, 'bot_cleanup_failed', 'Telegram API rejected removal')
                    """,
                    chat_id, user_id, call.from_user.id,
                )
            except Exception:
                logger.exception("Could not write bot-cleanup failure log")

    remaining = await _pool.fetchval(
        "SELECT COUNT(*) FROM known_group_bots WHERE chat_id = $1 AND removed_at IS NULL",
        chat_id,
    )
    summary = (
        "Очистка завершена.\n"
        f"Удалено: {removed}\n"
        f"Уже отсутствовали: {already_absent}\n"
        f"Боты-администраторы пропущены: {skipped_admins}\n"
        f"Ошибки Telegram: {failed}\n"
        f"Осталось в очереди: {remaining}"
    )
    if len(candidates) == BATCH_SIZE:
        summary += "\nОбработана партия из 100 аккаунтов. Нажмите очистку ещё раз для следующей партии."
    if call.message:
        await call.message.answer(summary)
    await call.answer("Очистка выполнена.", show_alert=True)
