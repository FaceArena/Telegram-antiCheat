import asyncio
import html
import logging
import secrets
import time

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


async def unrestrict(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Снять только ограничение, совпадающее с шаблоном CAPTCHA.

    Telegram не сообщает автора ограничения. Поэтому любое отличие от
    точного шаблона CAPTCHA трактуем как возможное вмешательство админа
    и не перезаписываем права участника.
    """
    member = await bot.get_chat_member(chat_id, user_id)

    # Если участник уже не ограничен, CAPTCHA не должна менять его права.
    if member.status != ChatMemberStatus.RESTRICTED:
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )

    # aiogram ChatMemberRestricted exposes permission flags directly; it has
    # no nested .permissions object (unlike Chat.get_chat().permissions).
    # Telegram/aiogram may omit optional permission flags in ChatMemberRestricted.
    # Missing (None) is not evidence that an administrator changed permissions;
    # an explicit True is. can_send_messages must still be explicitly False,
    # otherwise the member is already writable or the restriction was altered.
    if (
        getattr(member, "can_send_messages", None) is not False
        or any(getattr(member, field, False) is True for field in _FALLBACK_PERMS)
    ):
        logging.warning(
            "Капча: права участника изменились во время проверки; "
            "не снимаем возможное ограничение администратора "
            "(chat_id=%s, user_id=%s)",
            chat_id,
            user_id,
        )
        return False

    chat = await bot.get_chat(chat_id)
    perms = chat.permissions or ChatPermissions(**{f: True for f in _FALLBACK_PERMS})
    await bot.restrict_chat_member(chat_id, user_id, permissions=perms)
    return True


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


async def update_captcha_message(call: CallbackQuery, text: str):
    """Обновляет сообщение CAPTCHA после того, как callback уже подтверждён."""
    if call.message is None:
        return
    try:
        await call.message.edit_text(text, reply_markup=None)
    except TelegramAPIError:
        logging.warning(
            "Капча: не удалось обновить сообщение после callback",
            exc_info=True,
        )


def captcha_restricted_permissions() -> ChatPermissions:
    """Запрещает новичку все виды отправки до прохождения CAPTCHA."""
    return ChatPermissions(**{field: False for field in _FALLBACK_PERMS})


# ---------- Вход и выход участников ----------
@router.chat_member(F.chat.type.in_(GROUPS))
async def _temporary_restrict_on_storage_failure(bot: Bot, chat_id: int, user_id: int) -> None:
    """Fail closed briefly when CAPTCHA state cannot be persisted.

    The restriction expires automatically, so a database outage cannot create
    an unbounded lockout that only an operator can clear.
    """
    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=captcha_restricted_permissions(),
            until_date=int(time.time()) + CAPTCHA_TIMEOUT,
        )
    except TelegramAPIError:
        logging.exception(
            "CAPTCHA storage failed and temporary restriction also failed "
            "(chat_id=%s, user_id=%s)",
            chat_id,
            user_id,
        )


async def on_member_update(event: ChatMemberUpdated, bot: Bot):
    chat_id = event.chat.id
    user = event.new_chat_member.user
    old = event.old_chat_member.status
    new = event.new_chat_member.status

    # Ушёл до проверки: чистим заявку и сообщение
    if new in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        row = await common.pool.fetchrow(
            """
            DELETE FROM captcha_pending
            WHERE chat_id = $1 AND user_id = $2 AND created_at <= $3
            RETURNING message_id
            """,
            chat_id, user.id, event.date,
        )
        if row:
            await safe_delete(bot, chat_id, row["message_id"])
        return

    joined = old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new == ChatMemberStatus.MEMBER
    if not joined or user.is_bot:
        return

    try:
        settings = await get_settings(chat_id)
    except Exception:
        logging.exception(
            "CAPTCHA: cannot load group settings; temporarily restricting newcomer "
            "(chat_id=%s, user_id=%s)",
            chat_id,
            user.id,
        )
        await _temporary_restrict_on_storage_failure(bot, chat_id, user.id)
        return
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

    # Токен не позволяет старой кнопке завершить новую CAPTCHA того же
    # пользователя, даже пока message_id новой кнопки ещё не записан в БД.
    challenge_token = secrets.token_urlsafe(9)

    # Сначала резервируем проверку в БД. Это закрывает гонку, когда
    # пользователь нажимает кнопку раньше, чем запись CAPTCHA сохранена.
    try:
        pending = await common.pool.fetchrow(
            """
            INSERT INTO captcha_pending (
                chat_id, user_id, message_id, expires_at, challenge_token
            )
            VALUES ($1, $2, 0, now() + make_interval(secs => $3), $4)
            ON CONFLICT (chat_id, user_id) DO NOTHING
            RETURNING user_id
            """,
            chat_id,
            user.id,
            float(CAPTCHA_TIMEOUT),
            challenge_token,
        )
    except Exception:
        logging.exception(
            "Капча: не удалось зарезервировать проверку; временно ограничиваем новичка "
            "(chat_id=%s, user_id=%s)",
            chat_id,
            user.id,
        )
        await _temporary_restrict_on_storage_failure(bot, chat_id, user.id)
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
        # применить ограничение. Timeout-worker передаст случай на ручное решение админу.
        logging.exception(
            "Капча: не удалось подтвердить ограничение участника "
            "(chat_id=%s, user_id=%s)",
            chat_id,
            user.id,
        )
        await write_log(chat_id, user.id, "captcha_restriction_failed")
        return

    name = html.escape(user.full_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Я не бот", callback_data=f"c:{chat_id}:{user.id}:{challenge_token}",
    )]])
    try:
        msg = await bot.send_message(
            chat_id,
            f'<a href="tg://user?id={user.id}">{name}</a>, нажмите кнопку в течение '
            f"{CAPTCHA_TIMEOUT} сек., иначе доступ к сообщениям останется ограничен "
            "до решения администратора.",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        # Не снимаем ограничение, если не удалось показать проверку: это
        # превратило бы сбой Telegram API в обход CAPTCHA. Pending остаётся,
        # и timeout-worker завершит заявку, оставив решение администратору.
        logging.exception(
            "Капча: не удалось отправить проверку; участник останется ограничен "
            "до решения администратора (chat_id=%s, user_id=%s)",
            chat_id,
            user.id,
        )
        await write_log(chat_id, user.id, "captcha_prompt_failed")
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
        await write_log(
            chat_id,
            user.id,
            "captcha_started",
            f"timeout={CAPTCHA_TIMEOUT}s; anti_raid={raid_active}",
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
        parts = call.data.split(":")
        if len(parts) not in (3, 4):
            raise ValueError
        _, raw_chat, raw_user = parts[:3]
        challenge_token = parts[3] if len(parts) == 4 else None
        chat_id, user_id = int(raw_chat), int(raw_user)
    except (AttributeError, ValueError):
        return await call.answer()

    if call.from_user.id != user_id:
        return await call.answer("Эта кнопка не для вас.", show_alert=True)
    if call.message is None or call.message.chat.id != chat_id:
        return await call.answer("Кнопка недействительна.", show_alert=True)

    # Telegram ждёт answerCallbackQuery; подтверждаем нажатие ДО обращений к БД
    # и Telegram API, иначе клиент показывает бесконечную загрузку.
    await call.answer("Проверяем…")

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
            RETURNING message_id, challenge_token
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
        await update_captcha_message(call, "Не удалось проверить статус. Попробуйте позже.")
        return

    if not row:
        await update_captcha_message(
            call,
            "Время вышло, проверка уже выполняется или завершена.",
        )
        return
    if challenge_token is None:
        valid_button = (
            row["challenge_token"] is None
            and row["message_id"] > 0
            and call.message.message_id == row["message_id"]
        )
    else:
        valid_button = (
            row["challenge_token"] == challenge_token
            and (not row["message_id"] or call.message.message_id == row["message_id"])
        )
    if not valid_button:
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
                "Капча: не удалось освободить lease для устаревшей кнопки "
                "(chat_id=%s, user_id=%s)",
                chat_id,
                user_id,
            )
        await update_captcha_message(call, "Эта кнопка уже недействительна.")
        return

    try:
        restored = await unrestrict(bot, chat_id, user_id)
        if not restored:
            await common.pool.execute(
                """
                UPDATE captcha_pending SET processing_until = NULL
                WHERE chat_id = $1 AND user_id = $2
                """,
                chat_id,
                user_id,
            )
            await update_captcha_message(
                call,
                "Права изменились во время проверки. Администратор должен проверить участника.",
            )
            return
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
        await update_captcha_message(
            call,
            "Не удалось завершить проверку. Попробуйте ещё раз.",
        )
        return

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
            # Telegram уже подтвердил успешное снятие CAPTCHA-ограничения.
            # Не накладываем его повторно из-за ошибки БД: администратор мог
            # изменить права после успешной проверки. Оставшаяся pending-запись
            # позволяет повторить очистку при следующем нажатии.
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
                    "Капча: не удалось освободить lease после ошибки удаления "
                    "(chat_id=%s, user_id=%s)",
                    chat_id,
                    user_id,
                )
            await update_captcha_message(
                call,
                "Проверка пройдена, доступ восстановлен. Запись будет очищена повторно.",
            )
            return

    await write_log(chat_id, user_id, "captcha_passed")
    await safe_delete(bot, chat_id, row["message_id"] or call.message.message_id)


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
                    pending.attempts
                """
            )

            for candidate in rows:
                chat_id = candidate["chat_id"]
                user_id = candidate["user_id"]
                message_id = candidate["message_id"]
                completed = False

                try:
                    member = await bot.get_chat_member(chat_id, user_id)
                    # Telegram не сообщает, кто установил статус KICKED.
                    # Поэтому нельзя безопасно делать unban даже при kick_started:
                    # администратор мог забанить пользователя между двумя API-вызовами.
                    # Автоматический kick отключён: активный пользователь остаётся
                    # в текущем состоянии, а администратор решает вручную.
                    completed = member.status in (
                        ChatMemberStatus.LEFT,
                        ChatMemberStatus.KICKED,
                        ChatMemberStatus.MEMBER,
                        ChatMemberStatus.ADMINISTRATOR,
                        ChatMemberStatus.CREATOR,
                        ChatMemberStatus.RESTRICTED,
                    )
                except TelegramAPIError:
                    logging.exception(
                        "Капча: Telegram API ошибка при проверке таймаута "
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
                    await write_log(
                        chat_id,
                        user_id,
                        "captcha_failed_manual_review",
                        "Таймаут CAPTCHA. Автоматический kick отключён, "
                        "чтобы не затронуть бан администратора; требуется решение админа.",
                    )
                    await safe_delete(bot, chat_id, message_id)
                else:
                    # Ограниченный backoff: 10, 20, 40... максимум 300 секунд.
                    retry_delay = min(
                        300,
                        10 * (2 ** min(max(candidate["attempts"] - 1, 0), 5)),
                    )
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
