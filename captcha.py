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

import antiraid
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
    if not settings:
        return

    inviter = event.from_user
    inviter_is_admin = False
    if inviter.id != user.id:
        try:
            inviter_is_admin = await is_chat_admin(bot, chat_id, inviter.id)
        except TelegramAPIError:
            # Неизвестный статус не даёт обходить CAPTCHA и не исключает
            # вступление из подсчёта рейда.
            logging.warning(
                "Капча: не удалось проверить пригласившего %s в %s; "
                "новичок пройдёт проверку",
                inviter.id,
                chat_id,
                exc_info=True,
            )

    raid_active = False
    if settings["anti_raid"]:
        try:
            if inviter_is_admin:
                # Не учитываем доверенные массовые приглашения в пороге рейда,
                # но не позволяем им обходить уже активный защитный режим.
                raid_active = await antiraid.is_active(common.pool, chat_id)
            else:
                join_count, raid_active, raid_triggered = await antiraid.register_join(
                    common.pool,
                    chat_id,
                    user.id,
                    settings["raid_join_limit"],
                    settings["raid_window"],
                    settings["raid_lockdown_seconds"],
                )
                if raid_triggered:
                    await write_log(
                        chat_id,
                        user.id,
                        "raid_detected",
                        f"{join_count} уникальных вступлений за {settings['raid_window']} сек.; "
                        f"режим защиты на {settings['raid_lockdown_seconds']} сек.",
                    )
                    logging.warning(
                        "Обнаружен рейд в чате %s: %s уникальных вступлений за %s сек.",
                        chat_id,
                        join_count,
                        settings["raid_window"],
                    )
        except Exception:
            # Не блокируем чат при отказе БД. Обычная CAPTCHA продолжает работать.
            logging.exception(
                "Anti-raid не смог обработать вступление (chat_id=%s, user_id=%s)",
                chat_id,
                user.id,
            )

    # Anti-raid требует проверки от обычных новичков даже до достижения
    # порога: это закрывает окно, в котором первые участники волны могли бы писать.
    captcha_required = bool(settings["captcha"] or settings["anti_raid"] or raid_active)
    if not captcha_required:
        return

    # Админские приглашения проходят штатно; во время активного рейда
    # даже они получают проверку, чтобы не было обхода через массовые инвайты.
    if inviter_is_admin and not raid_active:
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
    except (AttributeError, ValueError):
        return await call.answer()

    if call.from_user.id != user_id:
        return await call.answer("Эта кнопка не для вас.", show_alert=True)
    if call.message is None or call.message.chat.id != chat_id:
        return await call.answer("Кнопка недействительна.", show_alert=True)

    try:
        # Атомарно забираем право обработать callback. Транзакция завершается
        # до вызова Telegram API, поэтому строка БД не удерживается на сети.
        row = await common.pool.fetchrow(
            """
            UPDATE captcha_pending
            SET processing_until = now() + interval '2 minutes'
            WHERE chat_id = $1
              AND user_id = $2
              AND expires_at > now()
              AND (processing_until IS NULL OR processing_until < now())
            RETURNING message_id
            """,
            chat_id,
            user_id,
        )
    except Exception:
        logging.exception(
            "Капча: не удалось захватить callback (chat_id=%s, user_id=%s)",
            chat_id,
            user_id,
        )
        return await call.answer(
            "Не удалось проверить статус. Попробуйте позже.",
            show_alert=True,
        )

    if not row:
        return await call.answer(
            "Время вышло, проверка уже выполняется или завершена.",
            show_alert=True,
        )
    if row["message_id"] and call.message.message_id != row["message_id"]:
        await common.pool.execute(
            """
            UPDATE captcha_pending SET processing_until = NULL
            WHERE chat_id = $1 AND user_id = $2
            """,
            chat_id,
            user_id,
        )
        return await call.answer("Эта кнопка уже недействительна.", show_alert=True)

    try:
        await unrestrict(bot, chat_id, user_id)
    except TelegramAPIError:
        logging.exception(
            "Капча: Telegram не снял ограничения (chat_id=%s, user_id=%s)",
            chat_id,
            user_id,
        )
        try:
            await common.pool.execute(
                """
                UPDATE captcha_pending SET processing_until = NULL
                WHERE chat_id = $1 AND user_id = $2
                """,
                chat_id,
                user_id,
            )
        except Exception:
            logging.exception(
                "Капча: не удалось освободить callback lease "
                "(chat_id=%s, user_id=%s)",
                chat_id,
                user_id,
            )
        return await call.answer(
            "Не удалось завершить проверку. Попробуйте ещё раз.",
            show_alert=True,
        )

    try:
        await common.pool.execute(
            "DELETE FROM captcha_pending WHERE chat_id = $1 AND user_id = $2",
            chat_id,
            user_id,
        )
    except Exception:
        logging.exception(
            "Капча: ошибка удаления успешной проверки (chat_id=%s, user_id=%s)",
            chat_id,
            user_id,
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
                user_id,
            )
        except Exception:
            logging.exception(
                "Капча: не удалось проверить pending после ошибки удаления "
                "(chat_id=%s, user_id=%s)",
                chat_id,
                user_id,
            )
            pending_exists = True

        if pending_exists:
            try:
                await bot.restrict_chat_member(
                    chat_id,
                    user_id,
                    permissions=captcha_restricted_permissions(),
                )
                await common.pool.execute(
                    """
                    UPDATE captcha_pending SET processing_until = NULL
                    WHERE chat_id = $1 AND user_id = $2
                    """,
                    chat_id,
                    user_id,
                )
            except Exception:
                logging.exception(
                    "Капча: не удалось вернуть ограничение/освободить задачу "
                    "(chat_id=%s, user_id=%s)",
                    chat_id,
                    user_id,
                )
            return await call.answer(
                "Не удалось сохранить результат. Попробуйте ещё раз.",
                show_alert=True,
            )

    await write_log(chat_id, user_id, "captcha_passed")
    await call.answer("Добро пожаловать!")
    await safe_delete(bot, chat_id, row["message_id"])


# ---------- Фоновая проверка таймаутов ----------
async def captcha_worker(bot: Bot):
    while True:
        try:
            # Короткая атомарная выдача lease. После этого SQL-запроса нет
            # открытой транзакции/row lock во время Telegram API вызовов.
            rows = await common.pool.fetch(
                """
                WITH due AS (
                    SELECT chat_id, user_id
                    FROM captcha_pending
                    WHERE expires_at <= now()
                      AND (processing_until IS NULL OR processing_until < now())
                      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                    ORDER BY expires_at
                    LIMIT 100
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE captcha_pending AS pending
                SET processing_until = now() + interval '2 minutes',
                    attempts = pending.attempts + 1
                FROM due
                WHERE pending.chat_id = due.chat_id
                  AND pending.user_id = due.user_id
                RETURNING
                    pending.chat_id,
                    pending.user_id,
                    pending.message_id,
                    pending.attempts,
                    pending.kick_started
                """
            )

            for candidate in rows:
                chat_id = candidate["chat_id"]
                user_id = candidate["user_id"]
                message_id = candidate["message_id"]
                completed = False
                kick_started = bool(candidate["kick_started"])

                try:
                    member = await bot.get_chat_member(chat_id, user_id)
                    if member.status == ChatMemberStatus.LEFT:
                        completed = True
                    elif member.status == ChatMemberStatus.KICKED:
                        # Не снимаем бан, выставленный администратором вручную.
                        # Разбаниваем только если наш worker ранее начал kick.
                        if kick_started:
                            await bot.unban_chat_member(
                                chat_id,
                                user_id,
                                only_if_banned=True,
                            )
                        completed = True
                    else:
                        # Фиксируем намерение до Telegram API. Если процесс
                        # упадёт между ban и unban, следующий запуск поймёт,
                        # что KICKED был результатом нашей операции.
                        await common.pool.execute(
                            """
                            UPDATE captcha_pending
                            SET kick_started = TRUE
                            WHERE chat_id = $1 AND user_id = $2
                              AND processing_until > now()
                            """,
                            chat_id,
                            user_id,
                        )
                        kick_started = True
                        await kick(bot, chat_id, user_id)
                        completed = True
                except TelegramAPIError:
                    logging.exception(
                        "Капча: Telegram API ошибка при удалении "
                        "(chat_id=%s, user_id=%s)",
                        chat_id,
                        user_id,
                    )
                    # Проверяем результат неоднозначного ban/unban. Если
                    # пользователь уже вышел, завершаем задачу. Если наш ban
                    # успел примениться, пытаемся снять именно его.
                    try:
                        member = await bot.get_chat_member(chat_id, user_id)
                        if member.status == ChatMemberStatus.LEFT:
                            completed = True
                        elif member.status == ChatMemberStatus.KICKED and kick_started:
                            await bot.unban_chat_member(
                                chat_id,
                                user_id,
                                only_if_banned=True,
                            )
                            completed = True
                    except TelegramAPIError:
                        logging.exception(
                            "Капча: не удалось проверить состояние после ошибки API "
                            "(chat_id=%s, user_id=%s)",
                            chat_id,
                            user_id,
                        )

                if completed:
                    await common.pool.execute(
                        """
                        DELETE FROM captcha_pending
                        WHERE chat_id = $1 AND user_id = $2
                        """,
                        chat_id,
                        user_id,
                    )
                    await write_log(chat_id, user_id, "captcha_failed")
                    await safe_delete(bot, chat_id, message_id)
                else:
                    # Ограниченный backoff: 10, 20, 40... максимум 300 секунд.
                    retry_delay = min(300, 10 * (2 ** min(candidate["attempts"], 5)))
                    await common.pool.execute(
                        """
                        UPDATE captcha_pending
                        SET processing_until = NULL,
                            next_attempt_at = now() + make_interval(secs => $3)
                        WHERE chat_id = $1 AND user_id = $2
                        """,
                        chat_id,
                        user_id,
                        float(retry_delay),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка в captcha_worker")

        await asyncio.sleep(5)
