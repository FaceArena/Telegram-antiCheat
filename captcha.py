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


async def safe_delete(bot: Bot, chat_id: int, message_id: int | None):
    if not message_id or message_id <= 0:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        logging.debug(
            "Капча: не удалось удалить сообщение %s в %s",
            message_id,
            chat_id,
            exc_info=True,
        )


def captcha_restricted_permissions() -> ChatPermissions:
    """Запрещает новичку все виды отправки до прохождения CAPTCHA."""
    return ChatPermissions(**{field: False for field in _FALLBACK_PERMS})


async def delete_pending(chat_id: int, user_id: int) -> None:
    await common.pool.execute(
        "DELETE FROM captcha_pending WHERE chat_id = $1 AND user_id = $2",
        chat_id,
        user_id,
    )


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

    # Сначала резервируем проверку в БД. Это закрывает гонку, когда
    # пользователь нажимает кнопку раньше, чем запись CAPTCHA сохранена.
    try:
        pending = await common.pool.fetchrow(
            """
            INSERT INTO captcha_pending (chat_id, user_id, message_id, expires_at)
            VALUES ($1, $2, 0, now() + make_interval(secs => $3))
            ON CONFLICT (chat_id, user_id) DO NOTHING
            RETURNING user_id
            """,
            chat_id,
            user.id,
            float(CAPTCHA_TIMEOUT),
        )
    except Exception:
        logging.exception(
            "Капча: не удалось зарезервировать проверку (chat_id=%s, user_id=%s)",
            chat_id,
            user.id,
        )
        return

    # Повторная доставка одного chat_member update не создаёт вторую CAPTCHA.
    if not pending:
        return

    try:
        await bot.restrict_chat_member(
            chat_id,
            user.id,
            permissions=captcha_restricted_permissions(),
        )
    except TelegramAPIError:
        # Не удаляем pending: при неоднозначной сетевой ошибке Telegram мог
        # применить ограничение. Worker завершит проверку и удалит участника.
        logging.exception(
            "Капча: не удалось подтвердить ограничение участника "
            "(chat_id=%s, user_id=%s)",
            chat_id,
            user.id,
        )
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
    except TelegramAPIError:
        logging.exception(
            "Капча: не удалось отправить сообщение (chat_id=%s, user_id=%s)",
            chat_id,
            user.id,
        )
        try:
            await unrestrict(bot, chat_id, user.id)
        except TelegramAPIError:
            # Оставляем pending: worker удалит участника, если восстановить
            # права не удалось. Иначе ограниченный пользователь не зависнет.
            logging.exception(
                "Капча: не удалось снять ограничение после ошибки отправки "
                "(chat_id=%s, user_id=%s)",
                chat_id,
                user.id,
            )
            return
        try:
            await delete_pending(chat_id, user.id)
        except Exception:
            # Ошибка клиента могла прийти уже после успешного DELETE на сервере.
            # Проверяем, осталась ли задача, прежде чем снова ограничивать человека.
            logging.exception(
                "Капча: ошибка удаления pending после ошибки отправки "
                "(chat_id=%s, user_id=%s)",
                chat_id,
                user.id,
            )
            try:
                pending_exists = await common.pool.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM captcha_pending
                        WHERE chat_id = $1 AND user_id = $2
                    )
                    """,
                    chat_id,
                    user.id,
                )
            except Exception:
                logging.exception(
                    "Капча: не удалось проверить pending после ошибки удаления "
                    "(chat_id=%s, user_id=%s)",
                    chat_id,
                    user.id,
                )
                pending_exists = True

            if pending_exists:
                try:
                    await bot.restrict_chat_member(
                        chat_id,
                        user.id,
                        permissions=captcha_restricted_permissions(),
                    )
                except TelegramAPIError:
                    logging.exception(
                        "Капча: не удалось восстановить ограничение после ошибки очистки "
                        "(chat_id=%s, user_id=%s)",
                        chat_id,
                        user.id,
                    )
        return

    try:
        await common.pool.execute(
            """
            UPDATE captcha_pending
            SET message_id = $3
            WHERE chat_id = $1 AND user_id = $2
            """,
            chat_id,
            user.id,
            msg.message_id,
        )
    except Exception:
        # Запись с message_id=0 уже существует, поэтому callback и timeout
        # остаются рабочими даже при ошибке обновления ID сообщения.
        logging.exception(
            "Капча: не удалось сохранить message_id (chat_id=%s, user_id=%s)",
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

    # Жать может только тот, для кого капча, и только из исходного чата.
    if call.from_user.id != user_id:
        return await call.answer("Эта кнопка не для вас.", show_alert=True)
    if call.message is None or call.message.chat.id != chat_id:
        return await call.answer("Кнопка недействительна.", show_alert=True)

    was_unrestricted = False
    try:
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
                # Старую кнопку нельзя использовать для новой CAPTCHA того же
                # пользователя. message_id=0 допустим только в коротком окне,
                # пока join-handler сохраняет ID отправленного сообщения.
                if row["message_id"] and call.message.message_id != row["message_id"]:
                    return await call.answer("Эта кнопка уже недействительна.", show_alert=True)

                try:
                    await unrestrict(bot, chat_id, user_id)
                    was_unrestricted = True
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
    except Exception:
        logging.exception(
            "Капча: ошибка фиксации успешной проверки (chat_id=%s, user_id=%s)",
            chat_id,
            user_id,
        )
        if was_unrestricted:
            # Commit мог пройти на сервере, даже если клиент получил сетевую
            # ошибку. Сначала выясняем, осталась ли задача в очереди.
            try:
                pending_exists = await common.pool.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM captcha_pending
                        WHERE chat_id = $1 AND user_id = $2
                    )
                    """,
                    chat_id,
                    user_id,
                )
            except Exception:
                logging.exception(
                    "Капча: не удалось проверить состояние pending после ошибки БД "
                    "(chat_id=%s, user_id=%s)",
                    chat_id,
                    user_id,
                )
                pending_exists = True

            if not pending_exists:
                await write_log(chat_id, user_id, "captcha_passed")
                await call.answer("Добро пожаловать!")
                await safe_delete(bot, chat_id, call.message.message_id)
                return

            try:
                await bot.restrict_chat_member(
                    chat_id,
                    user_id,
                    permissions=captcha_restricted_permissions(),
                )
            except TelegramAPIError:
                logging.exception(
                    "Капча: не удалось восстановить ограничение после ошибки БД "
                    "(chat_id=%s, user_id=%s); pending остаётся для worker",
                    chat_id,
                    user_id,
                )
        return await call.answer(
            "Не удалось сохранить результат проверки. Попробуйте ещё раз.",
            show_alert=True,
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
