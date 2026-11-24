"""Private-chat interface for browsing per-group audit events."""
from datetime import timezone

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message

import bot as app
from common import is_chat_admin
from protection import write_log

router = Router()
PAGE_SIZE = 10
FILTERS = {
    "all": ("Все события", None),
    "moderation": ("Модерация", [
        "flood", "stopword", "link_blocked", "mention_blocked",
        "bot_advertising", "bot_advertising_ban_failed",
        "punishment_mute", "punishment_ban", "punishment_delete",
    ]),
    "settings": ("Настройки", ["toggle", "punishment_changed", "private_setting_changed"]),
    "members": ("Участники", [
        "member_joined", "member_left", "member_banned", "member_unbanned",
        "member_restricted", "member_admin_changed", "captcha_passed",
        "captcha_failed_manual_review", "raid_detected",
    ]),
}
ACTION_LABELS = {
    "flood": "Антифлуд: обнаружен флуд",
    "stopword": "Обнаружено стоп-слово",
    "link_blocked": "Заблокирована ссылка",
    "mention_blocked": "Заблокировано упоминание",
    "bot_advertising": "Реклама бота: автор заблокирован",
    "bot_advertising_ban_failed": "Не удалось заблокировать рекламу бота",
    "punishment_mute": "Выдан мут",
    "punishment_ban": "Выдан бан",
    "punishment_delete": "Удалено сообщение",
    "toggle": "Изменён переключатель защиты",
    "punishment_changed": "Изменено наказание",
    "private_setting_changed": "Изменена настройка",
    "member_joined": "Участник вступил",
    "member_left": "Участник вышел",
    "member_banned": "Участник заблокирован",
    "member_unbanned": "Блокировка снята",
    "member_restricted": "Ограничены права",
    "member_admin_changed": "Изменён статус администратора",
    "captcha_passed": "CAPTCHA пройдена",
    "captcha_failed_manual_review": "CAPTCHA: требуется решение администратора",
    "raid_detected": "Обнаружен рейд",
}


def _keyboard(chat_id: int, offset: int, selected: str, has_next: bool):
    rows = [[InlineKeyboardButton(
        text=("• " if key == selected else "") + label,
        callback_data=f"audit:{chat_id}:0:{key}",
    ) for key, (label, _) in FILTERS.items()]]
    navigation = []
    if offset:
        navigation.append(InlineKeyboardButton(
            text="← Новее", callback_data=f"audit:{chat_id}:{max(0, offset - PAGE_SIZE)}:{selected}"
        ))
    if has_next:
        navigation.append(InlineKeyboardButton(
            text="Старее →", callback_data=f"audit:{chat_id}:{offset + PAGE_SIZE}:{selected}"
        ))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="← Мои группы", callback_data="audit_groups")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_group_picker(target_message: Message, user_id: int, bot):
    rows = await app.pool.fetch(
        "SELECT chat_id, title FROM chats WHERE active AND chat_type IN ('group', 'supergroup') ORDER BY title"
    )
    buttons = []
    for row in rows:
        try:
            allowed = await is_chat_admin(bot, row["chat_id"], user_id, fresh=True)
        except Exception:
            allowed = False
        if allowed:
            buttons.append([InlineKeyboardButton(
                text=f"📜 {row['title'][:45]}",
                callback_data=f"audit:{row['chat_id']}:0:all",
            )])
    if not buttons:
        return await target_message.answer(
            "Нет подключённых групп, где вы сейчас администратор.",
            reply_markup=app.private_main_keyboard(),
        )
    await target_message.answer(
        "Журнал событий\nВыберите группу:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "📜 Логи")
async def open_logs(message: Message):
    await _send_group_picker(message, message.from_user.id, message.bot)


@router.callback_query(F.data == "audit_groups")
async def back_to_groups(call: CallbackQuery):
    if call.message:
        await _send_group_picker(call.message, call.from_user.id, call.bot)
    await call.answer()


@router.callback_query(F.data.startswith("audit:"))
async def show_logs(call: CallbackQuery):
    try:
        _, raw_chat_id, raw_offset, selected = (call.data or "").split(":", 3)
        chat_id, offset = int(raw_chat_id), int(raw_offset)
        if offset < 0 or offset > 100_000 or selected not in FILTERS:
            raise ValueError
    except (TypeError, ValueError):
        return await call.answer("Некорректный запрос.", show_alert=True)

    try:
        allowed = await is_chat_admin(call.bot, chat_id, call.from_user.id, fresh=True)
    except Exception:
        return await call.answer("Не удалось проверить права. Попробуйте позже.", show_alert=True)
    if not allowed:
        return await call.answer("Нет доступа к журналу этой группы.", show_alert=True)

    chat = await app.pool.fetchrow("SELECT title FROM chats WHERE chat_id = $1 AND active", chat_id)
    if not chat:
        return await call.answer("Группа отключена или не найдена.", show_alert=True)

    _, actions = FILTERS[selected]
    if actions is None:
        rows = await app.pool.fetch(
            """
            SELECT user_id, action, details, created_at
            FROM logs WHERE chat_id = $1
            ORDER BY created_at DESC, id DESC LIMIT $2 OFFSET $3
            """, chat_id, PAGE_SIZE + 1, offset,
        )
    else:
        rows = await app.pool.fetch(
            """
            SELECT user_id, action, details, created_at
            FROM logs WHERE chat_id = $1 AND action = ANY($2::text[])
            ORDER BY created_at DESC, id DESC LIMIT $3 OFFSET $4
            """, chat_id, actions, PAGE_SIZE + 1, offset,
        )

    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    lines = [f"📜 Журнал: {chat['title'] or chat_id}", f"События {offset + 1}–{offset + len(rows)}."]
    if not rows:
        lines.append("\nЗаписей пока нет.")
    for row in rows:
        created = row["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        stamp = created.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
        action = ACTION_LABELS.get(row["action"], row["action"].replace("_", " "))
        user = f" · ID {row['user_id']}" if row["user_id"] is not None else ""
        details = " ".join((row["details"] or "").split())[:180]
        lines.append(f"\n{stamp} — {action}{user}" + (f"\n   {details}" if details else ""))

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3880] + "\n…"
    keyboard = _keyboard(chat_id, offset, selected, has_next)
    if call.message:
        try:
            await call.message.edit_text(text, reply_markup=keyboard)
        except Exception:
            await call.message.answer(text, reply_markup=keyboard)
    await call.answer()


@router.chat_member(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def log_member_status(event: ChatMemberUpdated):
    old, new = event.old_chat_member.status, event.new_chat_member.status
    user = event.new_chat_member.user
    if old == new:
        return
    if new == ChatMemberStatus.MEMBER and old in {
        ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED
    }:
        action = "member_joined"
    elif new == ChatMemberStatus.LEFT:
        action = "member_left"
    elif new == ChatMemberStatus.KICKED:
        action = "member_banned"
    elif old == ChatMemberStatus.KICKED and new in {
        ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED
    }:
        action = "member_unbanned"
    elif new == ChatMemberStatus.RESTRICTED:
        action = "member_restricted"
    elif new in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR} or old in {
        ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR
    }:
        action = "member_admin_changed"
    else:
        return
    actor = event.from_user.id if event.from_user else None
    details = f"{user.full_name} (@{user.username})" if user.username else user.full_name
    if actor is not None:
        details += f"; инициатор ID {actor}"
    await write_log(event.chat.id, user.id, action, details)
