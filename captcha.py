import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import common
from common import is_chat_admin
from protection import get_settings, write_log

router = Router()
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
CAPTCHA_TIMEOUT = 120  # секунд на нажатие кнопки

_FALLBACK_PERMS = [
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes",
    "can_send_polls", "can_send_other_messages", "can_add_web_page_previews",
]


async def unrestrict(bot: Bot, chat_id: int, user_id: int):
    """Вернуть пользователю права по умолчанию для этого чата."""
    chat = await bot.get_chat(chat_id)
    perms = chat.permissions or ChatPermissions(**{f: True for f in _FALLBACK_PERMS})
    await bot.restrict_chat_member(chat_id, user_id, permissions=perms)


async def kick(bot: Bot, chat_id: int, user_id: int):
    """Кик без постоянного бана: человек сможет зайти снова."""
    await bot.ban_chat_member(chat_id, user_id)
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


async def safe_delete(bot: Bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


# ---------- Вход и выход участников ----------
@router.chat_member(F.chat.type.in_(GROUPS))
async def on_member_update(event: ChatMemberUpdated, bot: Bot):
    chat_id = event.chat.id
    user = event.new_chat_member.user
    old = event.old_chat_member.status
    new = event.new_chat_member.status

    # Ушёл до проверки: чистим заявку и сообщение
    if new in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        row = await common.pool.fetchrow(
            "DELETE FROM captcha_pending WHERE chat_id = $1 AND user_id = $2 RETURNING message_id",
            chat_id, user.id,
        )
        if row:
            await safe_delete(bot, chat_id, row["message_id"])
        return

    joined = old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new == ChatMemberStatus.MEMBER
    if not joined or user.is_bot:
        return

    settings = await get_settings(chat_id)
    if not settings or not settings["captcha"]:
        return

    # Если пользователя добавил админ, капчу не показываем
    inviter = event.from_user
    if inviter.id != user.id:
        try:
            inviter_is_admin = await is_chat_admin(
                bot,
                chat_id,
                inviter.id,
                fresh=True,
            )
        except TelegramAPIError:
            # При неизвестном статусе не считаем пригласившего администратором:
            # новичок проходит обратимую CAPTCHA вместо полного обхода проверки.
            logging.warning(
                "Капча: не удалось проверить пригласившего %s в %s; "
                "новичку будет предложена проверка",
                inviter.id,
                chat_id,
                exc_info=True,
            )
            inviter_is_admin = False
        if inviter_is_admin:
            return

    try:
        await bot.restrict_chat_member(
            chat_id, user.id, permissions=ChatPermissions(can_send_messages=False)
        )
    except TelegramAPIError as e:
        logging.warning("Капча: нет прав ограничить %s в %s: %s", user.id, chat_id, e)
        return

    name = html.escape(user.full_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Я не бот", callback_data=f"c:{chat_id}:{user.id}",
    )]])
    try:
        msg = await bot.send_message(
            chat_id,
            f'<a href="tg://user?id={user.id}">{name}</a>, нажмите кнопку в течение '
            f"{CAPTCHA_TIMEOUT} сек., иначе вы будете удалены из чата.",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError as e:
        logging.warning("Капча: не удалось отправить сообщение в %s: %s", chat_id, e)
        try:
            await unrestrict(bot, chat_id, user.id)
        except TelegramAPIError:
            logging.exception(
                "Капча: не удалось снять ограничения после ошибки отправки "
                "(chat_id=%s, user_id=%s)",
                chat_id,
                user.id,
            )
        return

    try:
        await common.pool.execute(
            """
            INSERT INTO captcha_pending (chat_id, user_id, message_id, expires_at)
            VALUES ($1, $2, $3, now() + make_interval(secs => $4))
            ON CONFLICT (chat_id, user_id)
            DO UPDATE SET message_id = $3, expires_at = now() + make_interval(secs => $4)
            """,
            chat_id, user.id, msg.message_id, float(CAPTCHA_TIMEOUT),
        )
    except Exception:
        logging.exception(
            "Капча: не удалось сохранить проверку (chat_id=%s, user_id=%s)",
            chat_id,
            user.id,
        )
        await safe_delete(bot, chat_id, msg.message_id)
        try:
            await unrestrict(bot, chat_id, user.id)
        except TelegramAPIError:
            logging.exception(
                "Капча: не удалось снять ограничения после ошибки БД "
                "(chat_id=%s, user_id=%s)",
                chat_id,
                user.id,
            )


# ---------- Нажатие кнопки ----------
@router.callback_query(F.data.startswith("c:"))
async def on_captcha_click(call: CallbackQuery, bot: Bot):
    try:
        _, raw_chat, raw_user = call.data.split(":")
        chat_id, user_id = int(raw_chat), int(raw_user)
    except ValueError:
        return await call.answer()

    # Жать может только тот, для кого капча
    if call.from_user.id != user_id:
        return await call.answer("Эта кнопка не для вас.", show_alert=True)

    async with common.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT message_id
                FROM captcha_pending
                WHERE chat_id = $1 AND user_id = $2 AND expires_at > now()
                FOR UPDATE
                """,
                chat_id,
                user_id,
            )
            if not row:
                return await call.answer(
                    "Время вышло или проверка уже пройдена.",
                    show_alert=True,
                )

            try:
                await unrestrict(bot, chat_id, user_id)
            except TelegramAPIError as e:
                logging.warning(
                    "Капча: не удалось снять ограничения %s в %s: %s",
                    user_id,
                    chat_id,
                    e,
                )
                return await call.answer(
                    "Не удалось завершить проверку. Попробуйте ещё раз или обратитесь к админам.",
                    show_alert=True,
                )

            await conn.execute(
                "DELETE FROM captcha_pending WHERE chat_id = $1 AND user_id = $2",
                chat_id,
                user_id,
            )

    await write_log(chat_id, user_id, "captcha_passed")
    await call.answer("Добро пожаловать!")
    await safe_delete(bot, chat_id, row["message_id"])


# ---------- Фоновая проверка таймаутов ----------
async def captcha_worker(bot: Bot):
    while True:
        try:
            # Сначала читаем кандидатов, но не удаляем их: ошибка Telegram
            # не должна терять задачу и оставлять участника навсегда ограниченным.
            rows = await common.pool.fetch(
                """
                SELECT chat_id, user_id
                FROM captcha_pending
                WHERE expires_at <= now()
                ORDER BY expires_at
                LIMIT 100
                """
            )
            for candidate in rows:
                completed = False
                message_id = None

                # Блокировка строки сериализует timeout с нажатием CAPTCHA.
                async with common.pool.acquire() as conn:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            """
                            SELECT message_id
                            FROM captcha_pending
                            WHERE chat_id = $1 AND user_id = $2 AND expires_at <= now()
                            FOR UPDATE SKIP LOCKED
                            """,
                            candidate["chat_id"],
                            candidate["user_id"],
                        )
                        if not row:
                            continue

                        message_id = row["message_id"]
                        try:
                            await kick(
                                bot,
                                candidate["chat_id"],
                                candidate["user_id"],
                            )
                        except TelegramAPIError:
                            # Если участник сам вышел, задача больше не нужна.
                            try:
                                member = await bot.get_chat_member(
                                    candidate["chat_id"],
                                    candidate["user_id"],
                                )
                                if member.status == ChatMemberStatus.LEFT:
                                    completed = True
                                elif member.status == ChatMemberStatus.KICKED:
                                    # kick мог успеть забанить, но не успеть разбанить.
                                    await bot.unban_chat_member(
                                        candidate["chat_id"],
                                        candidate["user_id"],
                                        only_if_banned=True,
                                    )
                                    completed = True
                            except TelegramAPIError:
                                logging.exception(
                                    "Капча: не удалось проверить/завершить удаление "
                                    "(chat_id=%s, user_id=%s); повторим позже",
                                    candidate["chat_id"],
                                    candidate["user_id"],
                                )
                        else:
                            completed = True

                        if completed:
                            await conn.execute(
                                "DELETE FROM captcha_pending WHERE chat_id = $1 AND user_id = $2",
                                candidate["chat_id"],
                                candidate["user_id"],
                            )
                            await conn.execute(
                                """
                                INSERT INTO logs (chat_id, user_id, action, details)
                                VALUES ($1, $2, 'captcha_failed', '')
                                """,
                                candidate["chat_id"],
                                candidate["user_id"],
                            )

                if completed and message_id is not None:
                    await safe_delete(
                        bot,
                        candidate["chat_id"],
                        message_id,
                    )
        except Exception:
            logging.exception("Ошибка в captcha_worker")
        await asyncio.sleep(10)
