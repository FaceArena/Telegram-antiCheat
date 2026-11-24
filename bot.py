import asyncio
import logging
import os
from pathlib import Path

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
import common
import antiraid
import captcha
import moderation
import flood
import protection
from common import invalidate_admin_cache, is_chat_admin
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Message,
    BotCommand,
    MenuButtonCommands,
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
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        return

    status = event.new_chat_member.status
    if status in (
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
    ):
        await pool.execute(
            """
            UPDATE chats
            SET active = FALSE, bot_is_admin = FALSE
            WHERE chat_id = $1
            """,
            chat.id,
        )
        return

    if status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return

    is_admin = status == ChatMemberStatus.ADMINISTRATOR
    can_delete = bool(
        is_admin and getattr(event.new_chat_member, "can_delete_messages", False)
    )
    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    # A channel can be connected as a publishing destination, but it is not
    # an independently moderated member chat in Telegram Bot API.
    if chat.type == ChatType.CHANNEL:
        can_publish = bool(
            is_admin and getattr(event.new_chat_member, "can_post_messages", False)
        )
        channel_ready = is_admin and can_publish and can_delete
        await pool.execute(
            """
            INSERT INTO chats (chat_id, title, chat_type, added_by, bot_is_admin, active)
            VALUES ($1, $2, 'channel', $3, $4, FALSE)
            ON CONFLICT (chat_id) DO UPDATE
            SET title = EXCLUDED.title,
                chat_type = 'channel',
                added_by = COALESCE(chats.added_by, EXCLUDED.added_by),
                bot_is_admin = EXCLUDED.bot_is_admin,
                active = FALSE
            """,
            chat.id,
            chat.title or "",
            event.from_user.id,
            is_admin,
        )
        try:
            await bot.send_message(
                event.from_user.id,
                f"Канал «{chat.title or chat.id}» добавлен. "
                + (
                    "Права на публикацию и удаление постов подтверждены. "
                    if channel_ready
                    else "Не хватает прав на публикацию и/или удаление постов. "
                )
                + "Для модерации участников подключите группу обсуждений.",
                reply_markup=private_main_keyboard(),
            )
        except TelegramAPIError:
            logging.info(
                "Канал %s сохранён; личное уведомление пользователю %s недоставлено",
                chat.id,
                event.from_user.id,
            )
        return

    can_restrict = bool(
        is_admin and getattr(event.new_chat_member, "can_restrict_members", False)
    )
    fully_enabled = is_admin and can_delete and can_restrict

    # Group registration is a single upsert. Repeated permission-change events
    # must not replace the first installer or create duplicate settings rows.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO chats (chat_id, title, chat_type, added_by, bot_is_admin, active)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (chat_id) DO UPDATE
                SET title = EXCLUDED.title,
                    chat_type = EXCLUDED.chat_type,
                    added_by = COALESCE(chats.added_by, EXCLUDED.added_by),
                    bot_is_admin = EXCLUDED.bot_is_admin,
                    active = EXCLUDED.active
                """,
                chat.id,
                chat.title or "",
                str(chat.type),
                event.from_user.id,
                is_admin,
                fully_enabled,
            )
            await conn.execute(
                "INSERT INTO chat_settings (chat_id) VALUES ($1) ON CONFLICT DO NOTHING",
                chat.id,
            )

    try:
        await bot.send_message(
            event.from_user.id,
            f"Группа «{chat.title or chat.id}»: "
            + (
                "KimGuard подключён, обязательные права проверены. "
                "Откройте «Мои группы и каналы» для управления."
                if fully_enabled
                else "Бот добавлен, но защита не активирована. "
                     "Выдайте ему права удалять сообщения и ограничивать участников."
            ),
            reply_markup=private_main_keyboard(),
        )
    except TelegramAPIError:
        logging.info(
            "Группа %s сохранена; личное уведомление пользователю %s недоставлено",
            chat.id,
            event.from_user.id,
        )


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
def private_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 Мои группы и каналы")],
            [KeyboardButton(text="🛡 Мои защиты"), KeyboardButton(text="📖 Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие",
    )


async def accessible_chats(user_id: int, bot: Bot):
    rows = await pool.fetch(
        "SELECT chat_id, title FROM chats WHERE active AND chat_type IN ('group', 'supergroup') ORDER BY title"
    )
    accessible = []
    for row in rows:
        try:
            if await is_chat_admin(bot, row["chat_id"], user_id, fresh=True):
                accessible.append(row)
        except TelegramAPIError:
            logging.warning(
                "Не удалось проверить администратора %s в чате %s",
                user_id,
                row["chat_id"],
                exc_info=True,
            )
    return accessible


def build_admin_add_links(username: str) -> tuple[str, str]:
    """Telegram links request only the permissions required by KimGuard."""
    username = username.lstrip("@")
    if not username or not username.replace("_", "").isalnum():
        raise ValueError("Некорректное имя бота")
    return (
        f"https://t.me/{username}?startgroup&admin=delete_messages+restrict_members",
        f"https://t.me/{username}?startchannel&admin=post_messages+delete_messages",
    )


async def send_my_chats(message: Message, bot: Bot):
    me = await bot.me()
    group_url, channel_url = build_admin_add_links(me.username or "")
    add_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="＋ Добавить группу", url=group_url)],
        [InlineKeyboardButton(text="＋ Добавить канал", url=channel_url)],
    ])
    await message.answer(
        "Мои группы и каналы\nВыберите, что хотите подключить, или откройте уже подключённый чат:",
        reply_markup=add_keyboard,
    )
    rows = await pool.fetch(
        """
        SELECT chat_id, title, chat_type, active
        FROM chats
        WHERE active OR added_by = $1
        ORDER BY title
        """,
        message.from_user.id,
    )
    visible = []
    for row in rows:
        try:
            if await is_chat_admin(bot, row["chat_id"], message.from_user.id, fresh=True):
                visible.append(row)
        except TelegramAPIError:
            logging.warning(
                "Не удалось проверить доступ пользователя %s к чату %s",
                message.from_user.id, row["chat_id"], exc_info=True,
            )
    if not visible:
        return await message.answer(
            "Пока нет подключённых групп и каналов, где вы администратор.",
            reply_markup=private_main_keyboard(),
        )
    for row in visible:
        is_group = row["chat_type"] in ("group", "supergroup")
        if is_group and row["active"]:
            button = InlineKeyboardButton(text="Открыть настройки", callback_data=f"m:{row['chat_id']}")
            description = "🟢 Защита группы подключена."
        elif is_group:
            button = InlineKeyboardButton(text="Повторить выдачу прав", url=group_url)
            description = "🟠 Не хватает прав для полной защиты."
        elif row["active"]:
            button = InlineKeyboardButton(text="Информация", callback_data=f"c:{row['chat_id']}")
            description = "🟢 Канал подключён для публикаций. Модерация участников выполняется в группе обсуждений."
        else:
            button = InlineKeyboardButton(text="Подключить заново", url=channel_url)
            description = (
                "🟠 KimGuard не получил нужные права в канале или был удалён. "
                "Подключите его повторно."
            )
        await message.answer(
            f"📂 {row['title']}\n{description}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
        )


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
    descriptions = "\n\n".join(
        f"• {TOGGLES[field]} — {TOGGLE_DESCRIPTIONS[field]}"
        for field in TOGGLES
    )
    return (
        f"Настройки чата: {row['title']}{raid_info}\n\n{descriptions}",
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
    await message.answer(
        "Панель KimGuard доступна. Для перехода между разделами используйте кнопки меню.",
        reply_markup=private_main_keyboard(),
    )
    text, kb = menu
    await message.answer(text, reply_markup=kb)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_plain(message: Message, bot: Bot):
    await message.answer(
        "KimGuard — управление защитой групп и подключённых каналов.\n"
        "Подключение, настройки и журнал доступны здесь, в личном чате.",
        reply_markup=private_main_keyboard(),
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "📂 Мои группы и каналы")
async def menu_my_chats(message: Message, bot: Bot):
    await send_my_chats(message, bot)


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "🛡 Мои защиты")
async def menu_my_protections(message: Message, bot: Bot):
    rows = await accessible_chats(message.from_user.id, bot)
    if not rows:
        return await message.answer(
            "Нет подключённых групп с доступом администратора.",
            reply_markup=private_main_keyboard(),
        )
    for row in rows:
        record = await pool.fetchrow(
            """
            SELECT s.*, c.title
            FROM chat_settings s
            JOIN chats c USING (chat_id)
            WHERE s.chat_id = $1 AND c.active
            """,
            row["chat_id"],
        )
        if not record:
            continue
        enabled = [TOGGLES[key] for key in TOGGLES if record[key]]
        status = "\n".join(f"• {name}" for name in enabled) if enabled else "Пока ничего не включено."
        await message.answer(
            f"🛡 {record['title']}\n\nВключено:\n{status}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть настройки",
                    callback_data=f"m:{row['chat_id']}",
                )
            ]]),
        )


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "📖 Помощь")
async def menu_help(message: Message):
    await message.answer(
        "Как пользоваться KimGuard:\n"
        "1. Нажмите «📂 Мои группы и каналы» и выберите «Добавить группу».\n"
        "2. Выберите группу в Telegram и подтвердите права на удаление сообщений и блокировку участников.\n"
        "3. Выберите нужную группу в списке.\n"
        "4. Включайте нужные защиты в личной панели.\n\n"
        "Каждая защита включается отдельно. Вы сами выбираете, какие функции использовать.",
        reply_markup=private_main_keyboard(),
    )


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


@router.callback_query(F.data.startswith("c:"))
async def cb_channel_info(call: CallbackQuery, bot: Bot):
    try:
        chat_id = int((call.data or "").split(":", 1)[1])
        user_is_admin = await is_chat_admin(bot, chat_id, call.from_user.id, fresh=True)
        me = await bot.me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
    except (ValueError, TelegramAPIError):
        return await call.answer("Не удалось проверить доступ к каналу.", show_alert=True)
    if not user_is_admin:
        return await call.answer("Вы больше не администратор этого канала.", show_alert=True)
    if (
        bot_member.status != ChatMemberStatus.ADMINISTRATOR
        or not getattr(bot_member, "can_post_messages", False)
        or not getattr(bot_member, "can_delete_messages", False)
    ):
        return await call.answer("У KimGuard нет прав публиковать и удалять посты.", show_alert=True)
    await call.answer(
        "Права KimGuard проверены. Модерация участников доступна в группе обсуждений.",
        show_alert=True,
    )


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


# ---------- Очистка журнала ----------
LOG_RETENTION_DAYS = 90
LOG_CLEANUP_INTERVAL_SECONDS = 60 * 60
LOG_CLEANUP_BATCH_SIZE = 5000
LOG_CLEANUP_MAX_BATCHES = 20


async def cleanup_old_logs(db_pool) -> int:
    """Удаляет до 100 000 записей старше срока хранения за один запуск."""
    deleted_total = 0
    for _ in range(LOG_CLEANUP_MAX_BATCHES):
        status = await db_pool.execute(
            """
            WITH expired AS (
                SELECT id
                FROM logs
                WHERE created_at < now() - make_interval(days => $1)
                ORDER BY created_at
                LIMIT $2
            )
            DELETE FROM logs AS log
            USING expired
            WHERE log.id = expired.id
            """,
            LOG_RETENTION_DAYS,
            LOG_CLEANUP_BATCH_SIZE,
        )
        try:
            deleted = int(status.rsplit(" ", 1)[-1])
        except (AttributeError, ValueError) as exc:
            raise RuntimeError(f"Неожиданный ответ PostgreSQL при очистке логов: {status!r}") from exc
        deleted_total += deleted
        if deleted < LOG_CLEANUP_BATCH_SIZE:
            break
    return deleted_total


async def logs_retention_worker(db_pool):
    """Периодически ограничивает рост таблицы аудита."""
    while True:
        try:
            deleted = await cleanup_old_logs(db_pool)
            if deleted:
                logging.info(
                    "Очистка журнала: удалено %s записей старше %s дней.",
                    deleted,
                    LOG_RETENTION_DAYS,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка очистки старых записей журнала")
        await asyncio.sleep(LOG_CLEANUP_INTERVAL_SECONDS)


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
    log_retention_task = None
    flood_cleanup_task = None

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
        await bot.set_my_commands([
            BotCommand(command="start", description="Открыть меню"),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

        captcha_task = asyncio.create_task(
            captcha.captcha_worker(bot),
            name="captcha-worker",
        )
        anti_raid_task = asyncio.create_task(
            antiraid.anti_raid_worker(db_pool),
            name="anti-raid-worker",
        )
        log_retention_task = asyncio.create_task(
            logs_retention_worker(db_pool),
            name="logs-retention-worker",
        )
        flood_cleanup_task = asyncio.create_task(
            flood.flood_cleanup_worker(db_pool),
            name="flood-cleanup-worker",
        )
        await dp.start_polling(
            bot,
            allowed_updates=["message", "edited_message", "callback_query", "my_chat_member", "chat_member"],
            tasks_concurrency_limit=50,
            tasks_shutdown_timeout=30,
            close_bot_session=False,
        )
    finally:
        for task in (captcha_task, anti_raid_task, log_retention_task, flood_cleanup_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (captcha_task, anti_raid_task, log_retention_task, flood_cleanup_task) if task is not None),
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
