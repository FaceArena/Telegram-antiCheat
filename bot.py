import asyncio
import logging
import os
import time
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
from linkutils import normalize_host
from textfilters import normalize_text
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
    "block_mentions": "Блок упоминаний",
    "block_bot_mentions": "Автобан за рекламу ботов",
    "captcha": "Капча для новичков",
    "anti_raid": "Анти-рейд: проверка новичков",
    "antiflood": "Антифлуд",
    "stopwords": "Фильтр стоп-слов",
}
TOGGLE_DESCRIPTIONS = {
    "block_bots": "Удаляет ботов, которых добавили не администраторы. Ботов, приглашённых админами, не трогает.",
    "block_links": "Удаляет любые ссылки, включая скрытые ссылки и ссылки на каналы. Администраторы могут отправлять ссылки.",
    "block_mentions": "Удаляет упоминания публичных каналов через @username. Упоминания пользователей не блокирует; администраторы могут упоминать каналы.",
    "block_bot_mentions": "Удаляет сообщение и автоматически банит его автора за упоминание @username, оканчивающегося на bot или _bot. Администраторы исключены.",
    "captcha": "Требует от новых участников пройти CAPTCHA перед отправкой сообщений.",
    "anti_raid": "Отслеживает резкий наплыв участников и включает усиленную CAPTCHA-проверку новичков.",
    "antiflood": "Базовая защита всегда включена: более 5 сообщений за 5 секунд приводит к удалению пачки и муту. Здесь можно сделать порог строже; этот переключатель не отключает базовую защиту.",
    "stopwords": "Удаляет сообщения с фразами из списка. Добавление и удаление фраз выполняется кнопкой «Стоп-слова» в панели настроек группы; отдельных команд для этого нет.",
}
PUNISHMENTS = ["delete", "mute", "ban"]
PUNISH_LABELS = {"delete": "Удалить", "mute": "Мут", "ban": "Бан"}

router = Router()
pool: asyncpg.Pool
pending_admin_inputs: dict[int, tuple[int, str, float]] = {}


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
        await protection.write_log(
            chat.id,
            None,
            "bot_removed",
            f"new_status={status}",
            actor_user_id=event.from_user.id,
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
        can_edit = bool(
            is_admin and getattr(event.new_chat_member, "can_edit_messages", False)
        )
        channel_ready = is_admin and can_publish and can_edit and can_delete
        await pool.execute(
            """
            INSERT INTO chats (chat_id, title, chat_type, added_by, bot_is_admin, active)
            VALUES ($1, $2, 'channel', $3, $4, $5)
            ON CONFLICT (chat_id) DO UPDATE
            SET title = EXCLUDED.title,
                chat_type = 'channel',
                added_by = COALESCE(chats.added_by, EXCLUDED.added_by),
                bot_is_admin = EXCLUDED.bot_is_admin,
                active = EXCLUDED.active
            """,
            chat.id,
            chat.title or "",
            event.from_user.id,
            is_admin,
            channel_ready,
        )
        try:
            await bot.send_message(
                event.from_user.id,
                f"Канал «{chat.title or chat.id}» добавлен. "
                + (
                    "Права на публикацию, редактирование и удаление постов подтверждены. "
                    if channel_ready
                    else "Не хватает одного или нескольких прав на публикацию, редактирование и удаление постов. "
                )
                + "Основные фильтры и модерация участников сейчас доступны в группах обсуждений.",
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

    old_member = event.old_chat_member
    old_status = old_member.status
    old_ready = (
        old_status == ChatMemberStatus.ADMINISTRATOR
        and bool(getattr(old_member, "can_delete_messages", False))
        and bool(getattr(old_member, "can_restrict_members", False))
    )
    was_absent = old_status in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
        ChatMemberStatus.RESTRICTED,
    )
    if was_absent:
        await protection.write_log(
            chat.id,
            None,
            "bot_connected",
            f"title={chat.title or chat.id}; admin={is_admin}; delete={can_delete}; restrict={can_restrict}",
            actor_user_id=event.from_user.id,
        )
    # Notify only on meaningful state transitions: initial addition,
    # gaining all required rights, or losing them. Permission-change updates
    # with no effective state change must not spam the group.
    should_notify = was_absent or (fully_enabled != old_ready)

    if should_notify:
        try:
            if fully_enabled:
                me = await bot.me()
                await common.send_temporary_message(
                    bot,
                    chat.id,
                    f"✅ KimGuard успешно добавлен в группу «{chat.title or chat.id}». "
                    "Защита активирована: обязательные права подтверждены. "
                    "Настроить фильтры и правила можно в личной панели.",
                    reply_markup=build_settings_keyboard(me.username or "", chat.id),
                )
            else:
                await common.send_temporary_message(
                    bot,
                    chat.id,
                    f"⚠️ KimGuard добавлен в группу «{chat.title or chat.id}», "
                    "но защита пока не активирована. Назначьте бота администратором "
                    "и выдайте права удалять сообщения и ограничивать участников.",
                )
        except TelegramAPIError:
            logging.info(
                "Не удалось отправить уведомление о подключении в группу %s",
                chat.id,
                exc_info=True,
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
            [KeyboardButton(text="🛡 Мои защиты"), KeyboardButton(text="📜 Логи")],
            [KeyboardButton(text="📖 Помощь")],
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


def is_channel_info_callback(data: str | None) -> bool:
    """Match only the one-field channel-info callback, never CAPTCHA payloads."""
    if not isinstance(data, str) or not data.startswith("c:"):
        return False
    raw_chat_id = data[2:]
    if raw_chat_id.startswith("-"):
        raw_chat_id = raw_chat_id[1:]
    return bool(raw_chat_id) and raw_chat_id.isascii() and raw_chat_id.isdigit()


def build_settings_deep_link(username: str, chat_id: int) -> str:
    """Create a Telegram deep link to the private settings panel for one group."""
    username = username.lstrip("@")
    if not username or not username.isascii() or not all(
        char.isalnum() or char == "_" for char in username
    ):
        raise ValueError("Некорректное имя бота")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise ValueError("Некорректный ID чата")
    return f"https://t.me/{username}?start=settings_{chat_id}"


def build_settings_keyboard(username: str, chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⚙️ Настроить группу",
            url=build_settings_deep_link(username, chat_id),
        )
    ]])


def build_admin_add_links(username: str) -> tuple[str, str]:
    """Telegram links request only the permissions required by KimGuard."""
    username = username.lstrip("@")
    if not username or not username.isascii() or not all(
        char.isalnum() or char == "_" for char in username
    ):
        raise ValueError("Некорректное имя бота")
    return (
        f"https://t.me/{username}?startgroup&admin=delete_messages+restrict_members",
        f"https://t.me/{username}?startchannel&admin=post_messages+edit_messages+delete_messages",
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
        ORDER BY title
        """,
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
            description = "🟢 Канал подключён, право удаления постов проверено. Основные защиты доступны в группах."
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
    buttons.extend([
        [InlineKeyboardButton(
            text=f"Антифлуд: {row['flood_limit']} за {row['flood_window']} сек.",
            callback_data=f"f:{chat_id}",
        )],
        [InlineKeyboardButton(
            text=f"Стоп-слова ({len(row['stopword_list'])})",
            callback_data=f"s:{chat_id}",
        )],
    ])
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





LOG_ACTION_LABELS = {
    "bot_connected": "Бот подключён",
    "bot_banned": "Удалён посторонний бот",
    "bot_advertising": "Бан за рекламу бота",
    "bot_advertising_ban_failed": "Не удалось забанить за рекламу бота",
    "flood": "Обнаружен флуд",
    "stopword": "Сработал фильтр стоп-слов",
    "link_blocked": "Удалена запрещённая ссылка",
    "mention_blocked": "Удалено запрещённое упоминание",
    "captcha_passed": "CAPTCHA пройдена",
    "captcha_failed_manual_review": "CAPTCHA истекла — требуется проверка",
    "raid_detected": "Обнаружен рейд",
    "toggle": "Изменена настройка защиты",
    "punishment_changed": "Изменён тип наказания",
    "private_setting_changed": "Изменён параметр защиты",
    "member_joined": "Участник вступил",
    "member_left": "Участник вышел",
    "member_banned": "Участник заблокирован",
    "member_unbanned": "Блокировка участника снята",
    "member_muted": "Участник ограничен",
    "member_unmuted": "Ограничение участника снято",
    "member_status_changed": "Изменился статус участника",
    "bot_removed": "Бот удалён из группы",
}


async def send_logs_menu(message: Message, bot: Bot):
    rows = await accessible_chats(message.from_user.id, bot)
    if not rows:
        return await message.answer(
            "Нет групп, где вы администратор. Журнал доступен только администраторам подключённых групп.",
            reply_markup=private_main_keyboard(),
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📜 {row['title']}",
            callback_data=f"g:{row['chat_id']}:0",
        )]
        for row in rows
    ])
    await message.answer(
        "Выберите группу, журнал которой хотите открыть. Хранятся события за последние 90 дней.",
        reply_markup=keyboard,
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "📜 Логи")
async def menu_logs(message: Message, bot: Bot):
    await send_logs_menu(message, bot)


@router.callback_query(F.data.startswith("g:"))
async def cb_group_logs(call: CallbackQuery, bot: Bot):
    try:
        _, raw_chat_id, raw_page = (call.data or "").split(":")
        chat_id = int(raw_chat_id)
        page = int(raw_page)
        if page < 0 or page > 100_000:
            raise ValueError
    except (TypeError, ValueError):
        return await call.answer("Некорректная страница журнала.", show_alert=True)

    if chat_id == 0:
        chats = await accessible_chats(call.from_user.id, bot)
        if not chats:
            return await call.answer("Нет доступных групп.", show_alert=True)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📜 {row['title']}",
                callback_data=f"g:{row['chat_id']}:0",
            )]
            for row in chats
        ])
        if call.message:
            await call.message.edit_text("Выберите группу для просмотра журнала:", reply_markup=keyboard)
        return await call.answer()

    if not await _guard(call, bot, chat_id):
        return

    rows = await pool.fetch(
        """
        SELECT id, user_id, actor_user_id, target_user_id, action, details, created_at
        FROM logs
        WHERE chat_id = $1
          AND created_at >= now() - interval '90 days'
        ORDER BY id DESC
        LIMIT 11 OFFSET $2
        """,
        chat_id,
        page * 10,
    )
    has_next = len(rows) > 10
    rows = rows[:10]
    chat_title = await pool.fetchval("SELECT title FROM chats WHERE chat_id = $1", chat_id)
    if not rows:
        body = "Пока нет записей в журнале." if page == 0 else "На этой странице записей нет."
    else:
        entries = []
        for row in rows:
            timestamp = row["created_at"].strftime("%d.%m.%Y %H:%M:%S")
            action = LOG_ACTION_LABELS.get(row["action"], row["action"])
            target_id = row["target_user_id"] or (
                row["user_id"] if not row["actor_user_id"] else None
            )
            actor_id = row["actor_user_id"]
            who = f"Участник: {target_id}" if target_id else ""
            if actor_id:
                who = (who + f" · Инициатор: {actor_id}").strip(" ·")
            details = " ".join(str(row["details"] or "").split())[:180]
            line = f"• {timestamp} — {action}"
            if who:
                line += f"\n  {who}"
            if details:
                line += f"\n  {details}"
            entries.append(line)
        body = "\n\n".join(entries)

    text = f"📜 Журнал: {chat_title or chat_id}\nСтраница {page + 1}\n\n{body}"
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Новее", callback_data=f"g:{chat_id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Старее →", callback_data=f"g:{chat_id}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="← К списку групп", callback_data="g:0:0")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    if call.message:
        try:
            await call.message.edit_text(text, reply_markup=keyboard)
        except TelegramAPIError:
            await call.message.answer(text, reply_markup=keyboard)
    await call.answer()


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "📖 Помощь")
async def menu_help(message: Message):
    await message.answer(
        "Как пользоваться KimGuard:\n"
        "1. Откройте «📂 Мои группы и каналы» и добавьте группу.\n"
        "2. Назначьте бота администратором и выдайте права удалять сообщения и ограничивать участников.\n"
        "3. Выберите группу и управляйте защитами в личной панели.\n"
        "4. Антифлуд, стоп-слова и белый список ссылок настраиваются кнопками панели.\n"
        "5. В разделе «📜 Логи» доступны реальные события группы за последние 90 дней.\n\n"
        "Доступная команда Telegram: /start. Остальные действия выполняются кнопками меню.",
        reply_markup=private_main_keyboard(),
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def process_private_admin_input(message: Message, bot: Bot):
    pending = pending_admin_inputs.get(message.from_user.id)
    if not pending:
        return
    chat_id, action, expires_at = pending
    if time.monotonic() >= expires_at:
        pending_admin_inputs.pop(message.from_user.id, None)
        return await message.answer("Время ввода истекло. Откройте настройки снова.")
    try:
        is_admin = await is_chat_admin(bot, chat_id, message.from_user.id, fresh=True)
    except TelegramAPIError:
        return await message.answer("Не удалось проверить ваши права. Повторите попытку.")
    if not is_admin:
        pending_admin_inputs.pop(message.from_user.id, None)
        return await message.answer("Вы больше не администратор этой группы.")

    value = (message.text or "").strip()
    if value.casefold() in {"отмена", "cancel", "/cancel"}:
        pending_admin_inputs.pop(message.from_user.id, None)
        return await message.answer("Ввод отменён.")
    result_text = None

    if action == "flood":
        parts = value.split()
        if len(parts) != 2:
            return await message.answer("Введите два числа через пробел: лимит окно. Например: 5 10.")
        try:
            limit, window = map(int, parts)
        except ValueError:
            return await message.answer("Нужны два целых числа. Например: 5 10.")
        if not (2 <= limit <= 30 and 3 <= window <= 60):
            return await message.answer("Допустимые значения: лимит 2–30, окно 3–60 секунд.")
        status = await pool.execute(
            "UPDATE chat_settings SET flood_limit = $2, flood_window = $3 WHERE chat_id = $1",
            chat_id, limit, window,
        )
        if status != "UPDATE 1":
            return await message.answer("Не удалось сохранить настройки антифлуда.")
        result_text = f"Антифлуд настроен: {limit} сообщений за {window} секунд."

    elif action in ("stopword_add", "stopword_del"):
        word = normalize_text(value)
        if not 2 <= len(word) <= 64:
            return await message.answer("Введите фразу длиной от 2 до 64 символов.")
        if action == "stopword_add":
            status = await pool.execute(
                """
                UPDATE chat_settings
                SET stopword_list = array_append(stopword_list, $2)
                WHERE chat_id = $1
                  AND NOT ($2 = ANY(stopword_list))
                  AND coalesce(array_length(stopword_list, 1), 0) < 200
                """,
                chat_id, word,
            )
            if status != "UPDATE 1":
                return await message.answer("Фраза уже есть в списке или достигнут лимит 200.")
            result_text = f"Стоп-фраза добавлена: «{word}»."
        else:
            status = await pool.execute(
                """
                UPDATE chat_settings
                SET stopword_list = array_remove(stopword_list, $2)
                WHERE chat_id = $1 AND $2 = ANY(stopword_list)
                """,
                chat_id, word,
            )
            if status != "UPDATE 1":
                return await message.answer("Такой фразы нет в списке.")
            result_text = f"Стоп-фраза удалена: «{word}»."

    elif action in ("link_add", "link_del"):
        host = normalize_host(value)
        if not host or "." not in host:
            return await message.answer("Не удалось распознать домен. Пример: example.com")
        if action == "link_add":
            status = await pool.execute(
                """
                UPDATE chat_settings
                SET link_whitelist = array_append(link_whitelist, $2)
                WHERE chat_id = $1
                  AND NOT ($2 = ANY(link_whitelist))
                  AND coalesce(array_length(link_whitelist, 1), 0) < 100
                """,
                chat_id, host,
            )
            if status != "UPDATE 1":
                return await message.answer("Домен уже разрешён или достигнут лимит 100.")
            result_text = f"Домен {host} добавлен в белый список."
        else:
            status = await pool.execute(
                """
                UPDATE chat_settings
                SET link_whitelist = array_remove(link_whitelist, $2)
                WHERE chat_id = $1 AND $2 = ANY(link_whitelist)
                """,
                chat_id, host,
            )
            if status != "UPDATE 1":
                return await message.answer("Такого домена нет в белом списке.")
            result_text = f"Домен {host} удалён из белого списка."
    else:
        pending_admin_inputs.pop(message.from_user.id, None)
        return await message.answer("Сценарий ввода не найден. Откройте настройки заново.")

    pending_admin_inputs.pop(message.from_user.id, None)
    await pool.execute(
        """
        INSERT INTO logs (chat_id, user_id, action, details, actor_user_id)
        VALUES ($1, $2, 'private_setting_changed', $3, $2)
        """,
        chat_id, message.from_user.id, result_text,
    )
    await message.answer(result_text)
    menu = await build_menu(chat_id)
    if menu:
        text, keyboard = menu
        await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("f:"))
async def cb_flood_settings(call: CallbackQuery, bot: Bot):
    try:
        chat_id = int((call.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        return await call.answer("Некорректная команда.", show_alert=True)
    if not await _guard(call, bot, chat_id):
        return
    row = await pool.fetchrow(
        "SELECT flood_limit, flood_window FROM chat_settings WHERE chat_id = $1",
        chat_id,
    )
    if not row:
        return await call.answer("Настройки не найдены.", show_alert=True)
    pending_admin_inputs[call.from_user.id] = (chat_id, "flood", time.monotonic() + 600)
    if call.message:
        await call.message.answer(
            f"Текущий антифлуд: {row['flood_limit']} сообщений за {row['flood_window']} секунд.\n"
            "Отправьте новые значения через пробел: лимит окно.\n"
            "Допустимо: лимит 2–30, окно 3–60 секунд. Пример: 5 10. Для отмены отправьте «Отмена»."
        )
    await call.answer()


@router.callback_query(F.data.startswith("s:") | F.data.startswith("l:"))
async def cb_list_settings(call: CallbackQuery, bot: Bot):
    try:
        kind, raw_chat_id = (call.data or "").split(":", 1)
        chat_id = int(raw_chat_id)
    except (ValueError, TypeError):
        return await call.answer("Некорректная команда.", show_alert=True)
    if not await _guard(call, bot, chat_id):
        return
    if kind == "s":
        row = await pool.fetchrow(
            "SELECT stopword_list FROM chat_settings WHERE chat_id = $1", chat_id
        )
        if not row:
            return await call.answer("Список не найден.", show_alert=True)
        items = row["stopword_list"]
        title = "Стоп-слова"
        add_action, del_action = "stopword_add", "stopword_del"
    else:
        row = await pool.fetchrow(
            "SELECT link_whitelist FROM chat_settings WHERE chat_id = $1", chat_id
        )
        if not row:
            return await call.answer("Список не найден.", show_alert=True)
        items = row["link_whitelist"]
        title = "Белый список ссылок"
        add_action, del_action = "link_add", "link_del"
    shown = items[:30]
    listing = "\n".join(f"• {item}" for item in shown) if shown else "Список пуст."
    if len(items) > 30:
        listing += f"\n…и ещё {len(items) - 30}."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="＋ Добавить", callback_data=f"i:{chat_id}:{add_action}")],
        [InlineKeyboardButton(text="− Удалить", callback_data=f"i:{chat_id}:{del_action}")],
        [InlineKeyboardButton(text="← Назад", callback_data=f"m:{chat_id}")],
    ])
    if call.message:
        await call.message.answer(f"{title} ({len(items)}):\n{listing}", reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data.startswith("i:"))
async def cb_begin_admin_input(call: CallbackQuery, bot: Bot):
    try:
        _, raw_chat_id, action = (call.data or "").split(":", 2)
        chat_id = int(raw_chat_id)
    except (ValueError, TypeError):
        return await call.answer("Некорректная команда.", show_alert=True)
    if action not in {"stopword_add", "stopword_del", "link_add", "link_del"}:
        return await call.answer("Неизвестная настройка.", show_alert=True)
    if not await _guard(call, bot, chat_id):
        return
    pending_admin_inputs[call.from_user.id] = (chat_id, action, time.monotonic() + 600)
    prompts = {
        "stopword_add": "Отправьте стоп-слово или фразу (2–64 символа).",
        "stopword_del": "Отправьте точную фразу, которую нужно удалить из стоп-листа.",
        "link_add": "Отправьте домен для белого списка. Пример: example.com",
        "link_del": "Отправьте точный домен, который нужно удалить из белого списка.",
    }
    if call.message:
        await call.message.answer(prompts[action] + " Для отмены отправьте «Отмена».")
    await call.answer()


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




@router.chat_member(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def audit_member_status_change(event: ChatMemberUpdated):
    """Record real Telegram membership transitions in the selected group's audit log."""
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    target = event.new_chat_member.user
    if target.is_bot or old == new:
        return

    action = "member_status_changed"
    if new == ChatMemberStatus.MEMBER and old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        action = "member_joined" if old == ChatMemberStatus.LEFT else "member_unbanned"
    elif new == ChatMemberStatus.LEFT:
        action = "member_left"
    elif new == ChatMemberStatus.KICKED:
        action = "member_banned"
    elif new == ChatMemberStatus.RESTRICTED:
        action = "member_muted"
    elif old == ChatMemberStatus.RESTRICTED and new == ChatMemberStatus.MEMBER:
        action = "member_unmuted"

    actor_id = event.from_user.id if event.from_user.id != target.id else None
    details = f"name={target.full_name}; old={old}; new={new}"
    await protection.write_log(
        event.chat.id,
        target.id,
        action,
        details,
        actor_user_id=actor_id,
    )


@router.callback_query(F.data.func(is_channel_info_callback))
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
        or not getattr(bot_member, "can_edit_messages", False)
        or not getattr(bot_member, "can_delete_messages", False)
    ):
        return await call.answer("У KimGuard нет полных прав публиковать, редактировать и удалять посты в канале.", show_alert=True)
    await call.answer(
        "Права на публикацию, редактирование и удаление постов проверены. Фильтры участников и автоматическая модерация сейчас работают в группах обсуждений.",
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
                INSERT INTO logs (chat_id, user_id, action, details, actor_user_id)
                VALUES ($1, $2, 'toggle', $3, $2)
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
                INSERT INTO logs (chat_id, user_id, action, details, actor_user_id)
                VALUES ($1, $2, 'punishment_changed', $3, $2)
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
