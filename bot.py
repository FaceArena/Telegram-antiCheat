import asyncio
import logging
import os
from pathlib import Path

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart
import common
import antiraid
import captcha
import moderation
import protection
from common import invalidate_admin_cache, is_chat_admin
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# Белый список полей: только их можно переключать (защита от инъекций)
TOGGLES = {
    "block_bots": "Блок ботов",
    "block_links": "Блок ссылок",
    "captcha": "Капча для новичков",
    "anti_raid": "Анти-рейд: проверка новичков",
    "antiflood": "Антифлуд",
    "stopwords": "Фильтр стоп-слов",
}
TOGGLE_DESCRIPTIONS = {
    "block_bots": "Удаляет ботов, которых добавили не администраторы. Ботов, приглашённых админами, не трогает.",
    "block_links": "Удаляет ссылки, домены которых не добавлены в белый список. Администраторы могут отправлять ссылки.",
    "captcha": "Требует от новых участников пройти CAPTCHA перед отправкой сообщений.",
    "anti_raid": "Отслеживает резкий наплыв участников и включает усиленную CAPTCHA-проверку новичков.",
    "antiflood": "Удаляет сообщения сверх заданного лимита частоты. Лимит меняется командой /flood.",
    "stopwords": "Удаляет сообщения с фразами из списка. Управление: /addword, /delword и /words.",
}
PUNISHMENTS = ["delete", "mute", "ban"]
PUNISH_LABELS = {"delete": "Удалить", "mute": "Мут", "ban": "Бан"}

router = Router()
pool: asyncpg.Pool


# ---------- Подключение чата ----------
@router.my_chat_member()
async def on_bot_status_change(event: ChatMemberUpdated, bot: Bot):
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return  # каналы пока игнорируем, модерируем discussion-группу

    status = event.new_chat_member.status

    if status == ChatMemberStatus.RESTRICTED:
        await pool.execute(
            """
            UPDATE chats
            SET active = FALSE, bot_is_admin = FALSE
            WHERE chat_id = $1
            """,
            chat.id,
        )
        return

    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await pool.execute(
            """
            UPDATE chats
            SET active = FALSE, bot_is_admin = FALSE
            WHERE chat_id = $1
            """,
            chat.id,
        )
        return

    if status not in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    ):
        return

    is_admin = status == ChatMemberStatus.ADMINISTRATOR
    can_delete = bool(
        is_admin and getattr(event.new_chat_member, "can_delete_messages", False)
    )
    can_restrict = bool(
        is_admin and getattr(event.new_chat_member, "can_restrict_members", False)
    )
    fully_enabled = is_admin and can_delete and can_restrict

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO chats (chat_id, title, chat_type, added_by, bot_is_admin, active)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (chat_id) DO UPDATE
                SET title = $2, bot_is_admin = $5, active = $6
                """,
                chat.id,
                chat.title or "",
                chat.type.value,
                event.from_user.id,
                is_admin,
                fully_enabled,
            )
            await conn.execute(
                "INSERT INTO chat_settings (chat_id) VALUES ($1) ON CONFLICT DO NOTHING",
                chat.id,
            )

    me = await bot.me()
    if is_admin and can_delete and can_restrict:
        text = "Бот подключён. Настройки доступны только админам чата."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Настроить в личке",
            url=f"https://t.me/{me.username}?start=settings_{chat.id}",
        )]])
    elif is_admin:
        missing = []
        if not can_delete:
            missing.append("удалять сообщения")
        if not can_restrict:
            missing.append("ограничивать и блокировать участников")
        text = (
            "Бот добавлен как администратор, но ему не хватает прав: "
            + " и ".join(missing)
            + ". Включите эти разрешения в настройках администраторов."
        )
        kb = None
    else:
        text = "Сделайте меня администратором с правами удалять сообщения и ограничивать участников, иначе защита будет работать не полностью."
        kb = None
    try:
        await bot.send_message(chat.id, text, reply_markup=kb)
    except TelegramAPIError:
        logging.warning("Не удалось написать в чат %s", chat.id)


@router.chat_member(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    (
        F.old_chat_member.status.in_({
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        })
        | F.new_chat_member.status.in_({
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        })
    ),
)
async def on_member_status_change(event: ChatMemberUpdated):
    # Сбрасываем права пользователя сразу после изменения его статуса.
    await invalidate_admin_cache(event.chat.id, event.new_chat_member.user.id)


# ---------- Меню настроек ----------
async def build_menu(chat_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    row = await pool.fetchrow(
        """
        SELECT c.title, s.* FROM chat_settings s
        JOIN chats c USING (chat_id)
        WHERE s.chat_id = $1 AND c.active
        """,
        chat_id,
    )
    if not row:
        return None

    buttons = [
        [InlineKeyboardButton(
            text=f"{'✅' if row[field] else '❌'} {label}",
            callback_data=f"t:{chat_id}:{field}",
        )]
        for field, label in TOGGLES.items()
    ]
    buttons.append([InlineKeyboardButton(
        text=f"Наказание: {PUNISH_LABELS[row['punishment']]}",
        callback_data=f"p:{chat_id}",
    )])
    raid_info = ""
    if row["anti_raid"]:
        raid_info = (
            "\n\nАнти-рейд проверяет новых участников через CAPTCHA. "
            f"Порог: {row['raid_join_limit']} вступлений за {row['raid_window']} сек.; "
            f"усиленный режим после обнаружения действует {row['raid_lockdown_seconds']} сек."
        )
    descriptions = "\\n\\n".join(
        f"• {TOGGLES[field]} — {TOGGLE_DESCRIPTIONS[field]}"
        for field in TOGGLES
    )
    return (
        f"Настройки чата: {row['title']}{raid_info}\\n\\n{descriptions}",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(CommandStart(deep_link=True), F.chat.type == ChatType.PRIVATE)
async def start_with_payload(message: Message, command: CommandObject, bot: Bot):
    payload = command.args or ""
    if not payload.startswith("settings_"):
        return await message.answer("Неизвестная ссылка.")
    try:
        chat_id = int(payload.removeprefix("settings_"))
    except ValueError:
        return await message.answer("Неверная ссылка.")

    try:
        is_admin = await is_chat_admin(
            bot,
            chat_id,
            message.from_user.id,
            fresh=True,
        )
    except TelegramAPIError:
        logging.warning(
            "Не удалось проверить права пользователя %s в чате %s",
            message.from_user.id,
            chat_id,
            exc_info=True,
        )
        return await message.answer(
            "Не удалось проверить права администратора. Попробуйте позже."
        )
    if not is_admin:
        return await message.answer("Вы не админ этого чата.")

    menu = await build_menu(chat_id)
    if not menu:
        return await message.answer("Чат не найден. Добавьте бота в группу заново.")
    text, kb = menu
    await message.answer(text, reply_markup=kb)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_plain(message: Message, bot: Bot):
    rows = await pool.fetch(
        "SELECT chat_id, title FROM chats WHERE added_by = $1 AND active",
        message.from_user.id,
    )
    if not rows:
        return await message.answer(
            "Добавьте меня в группу и сделайте админом, затем нажмите «Настроить в личке»."
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r["title"] or str(r["chat_id"]), callback_data=f"m:{r['chat_id']}")]
        for r in rows
    ])
    await message.answer("Ваши чаты:", reply_markup=kb)


async def _guard(call: CallbackQuery, bot: Bot, chat_id: int) -> bool:
    # callback_data не доказательство прав, проверяем каждый раз заново.
    try:
        is_admin = await is_chat_admin(
            bot,
            chat_id,
            call.from_user.id,
            fresh=True,
        )
    except TelegramAPIError:
        logging.warning(
            "Не удалось проверить права для callback в чате %s",
            chat_id,
            exc_info=True,
        )
        await call.answer(
            "Не удалось проверить права. Попробуйте позже.",
            show_alert=True,
        )
        return False

    if not is_admin:
        await call.answer("Нет прав.", show_alert=True)
        return False
    return True


async def _refresh(call: CallbackQuery, chat_id: int):
    menu = await build_menu(chat_id)
    if menu and call.message:
        text, kb = menu
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except TelegramAPIError:
            pass  # «message is not modified»
    await call.answer()


@router.callback_query(F.data.startswith("m:"))
async def cb_menu(call: CallbackQuery, bot: Bot):
    try:
        parts = (call.data or "").split(":")
        if len(parts) != 2:
            raise ValueError
        chat_id = int(parts[1])
    except (TypeError, ValueError):
        return await call.answer("Некорректная команда.", show_alert=True)

    if await _guard(call, bot, chat_id):
        await _refresh(call, chat_id)


@router.callback_query(F.data.startswith("t:"))
async def cb_toggle(call: CallbackQuery, bot: Bot):
    try:
        parts = (call.data or "").split(":")
        if len(parts) != 3:
            raise ValueError
        _, raw_id, field = parts
        chat_id = int(raw_id)
    except (TypeError, ValueError):
        return await call.answer("Некорректная команда.", show_alert=True)

    if field not in TOGGLES:
        return await call.answer("Ошибка.", show_alert=True)
    if not await _guard(call, bot, chat_id):
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.fetchrow(
                f"""
                UPDATE chat_settings
                SET {field} = NOT {field}
                WHERE chat_id = $1
                  AND EXISTS (
                      SELECT 1 FROM chats
                      WHERE chats.chat_id = chat_settings.chat_id
                        AND chats.active
                  )
                RETURNING {field}
                """,
                chat_id,
            )
            if result is None:
                return await call.answer(
                    "Чат неактивен или настройки недоступны.",
                    show_alert=True,
                )
            await conn.execute(
                """
                INSERT INTO logs (chat_id, user_id, action, details)
                VALUES ($1, $2, 'toggle', $3)
                """,
                chat_id,
                call.from_user.id,
                f"{field}={'on' if result[field] else 'off'}",
            )
    await _refresh(call, chat_id)


@router.callback_query(F.data.startswith("p:"))
async def cb_punishment(call: CallbackQuery, bot: Bot):
    try:
        parts = (call.data or "").split(":")
        if len(parts) != 2:
            raise ValueError
        chat_id = int(parts[1])
    except (TypeError, ValueError):
        return await call.answer("Некорректная команда.", show_alert=True)

    if not await _guard(call, bot, chat_id):
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            cur = await conn.fetchval(
                """
                SELECT s.punishment
                FROM chat_settings s
                JOIN chats c USING (chat_id)
                WHERE s.chat_id = $1 AND c.active
                FOR UPDATE OF s
                """,
                chat_id,
            )
            if cur not in PUNISHMENTS:
                return await call.answer(
                    "Настройка наказания не найдена или чат неактивен.",
                    show_alert=True,
                )

            nxt = PUNISHMENTS[(PUNISHMENTS.index(cur) + 1) % len(PUNISHMENTS)]
            await conn.execute(
                "UPDATE chat_settings SET punishment = $2 WHERE chat_id = $1",
                chat_id,
                nxt,
            )
            await conn.execute(
                """
                INSERT INTO logs (chat_id, user_id, action, details)
                VALUES ($1, $2, 'punishment_changed', $3)
                """,
                chat_id,
                call.from_user.id,
                nxt,
            )
    await _refresh(call, chat_id)


# ---------- Запуск ----------
async def main():
    global pool
    logging.basicConfig(level=logging.INFO)

    bot_token = os.getenv("BOT_TOKEN")
    database_url = os.getenv("DATABASE_URL")
    if not bot_token or not database_url:
        raise RuntimeError("Не заданы обязательные переменные BOT_TOKEN и DATABASE_URL.")

    bot = Bot(bot_token)
    db_pool = None
    lock_conn = None
    captcha_task = None
    anti_raid_task = None

    try:
        # Один Telegram bot token должен иметь только один polling-процесс.
        # Соединение держим открытым: PostgreSQL advisory lock привязан к сессии.
        # Для этого DATABASE_URL должен быть direct/session-mode PostgreSQL URL,
        # не transaction-mode pooler.
        lock_conn = await asyncpg.connect(database_url)
        lock_acquired = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended('telegram-security-ai:polling', 0))"
        )
        if not lock_acquired:
            raise RuntimeError(
                "Другой экземпляр бота уже использует этот BOT_TOKEN и DATABASE_URL. "
                "Оставьте одну реплику polling-бота."
            )
        logging.info("Получена блокировка единственного polling-экземпляра.")

        db_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10)
        pool = db_pool
        common.pool = db_pool

        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        await db_pool.execute(schema)

        dp = Dispatcher()
        dp.include_router(router)
        # Сначала модерация: общий обработчик ссылок в protection.router
        # не должен перехватывать сообщения до антифлуда и стоп-слов.
        dp.include_router(moderation.router)
        dp.include_router(protection.router)
        dp.include_router(captcha.router)

        # Этот сервис работает через long polling. Удаляем возможный старый
        # webhook, не выбрасывая накопившиеся Telegram updates.
        await bot.delete_webhook(drop_pending_updates=False)

        captcha_task = asyncio.create_task(
            captcha.captcha_worker(bot),
            name="captcha-worker",
        )
        anti_raid_task = asyncio.create_task(
            antiraid.anti_raid_worker(db_pool),
            name="anti-raid-worker",
        )
        await dp.start_polling(
            bot,
            allowed_updates=["message", "edited_message", "callback_query", "my_chat_member", "chat_member"],
            tasks_concurrency_limit=50,
            tasks_shutdown_timeout=30,
            close_bot_session=False,
        )
    finally:
        for task in (captcha_task, anti_raid_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (captcha_task, anti_raid_task) if task is not None),
            return_exceptions=True,
        )
        try:
            if db_pool is not None:
                await db_pool.close()
        finally:
            try:
                if lock_conn is not None:
                    await lock_conn.close()  # закрытие сессии автоматически снимает advisory lock
            finally:
                await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
