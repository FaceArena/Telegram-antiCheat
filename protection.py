import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message

import common
from common import is_chat_admin
from linkutils import is_allowed, normalize_host

router = Router()
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
MUTE_HOURS = 1


# ---------- Вспомогательное ----------
async def get_settings(chat_id: int):
    return await common.pool.fetchrow(
        """
        SELECT s.* FROM chat_settings s
        JOIN chats c USING (chat_id)
        WHERE s.chat_id = $1 AND c.active
        """,
        chat_id,
    )


async def write_log(chat_id: int, user_id: int | None, action: str, details: str = ""):
    try:
        await common.pool.execute(
            "INSERT INTO logs (chat_id, user_id, action, details) VALUES ($1, $2, $3, $4)",
            chat_id, user_id, action, details[:500],
        )
    except Exception:
        # Ошибка аудита не должна прерывать уже выполненную модерацию
        # или блокировать обработку следующих участников.
        logging.exception(
            "Не удалось записать событие модерации "
            "(chat_id=%s, user_id=%s, action=%s)",
            chat_id,
            user_id,
            action,
        )


def is_anon_admin(message: Message) -> bool:
    """Анонимный админ пишет от имени самой группы."""
    return bool(message.sender_chat and message.sender_chat.id == message.chat.id)


async def apply_punishment(bot: Bot, chat_id: int, user_id: int, punishment: str):
    if punishment == "mute":
        until = datetime.now(timezone.utc) + timedelta(hours=MUTE_HOURS)
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
            until_date=until,
        )
    elif punishment == "ban":
        await bot.ban_chat_member(chat_id, user_id)
    # 'delete': сообщение уже удалено, больше ничего не делаем


async def notify_moderation(
    bot: Bot,
    chat_id: int,
    actor_name: str,
    punishment: str,
    reason: str,
    details: str = "",
) -> None:
    """Post a compact group notice after a moderation action actually succeeds."""
    action = {
        "ban": "заблокирован",
        "mute": "ограничен",
        "delete": "сообщение удалено",
    }.get(punishment, "обработан")
    details_text = f" ({details})" if details else ""
    try:
        await bot.send_message(
            chat_id,
            f"🛡 KimGuard: {actor_name} — {action}. Причина: {reason}{details_text}.",
        )
    except TelegramAPIError:
        logging.warning(
            "Не удалось отправить уведомление о модерации в чат %s",
            chat_id,
            exc_info=True,
        )


def find_hosts(message: Message) -> list[str | None]:
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    hosts: list[str | None] = []
    for e in entities:
        if e.type == MessageEntityType.URL:
            raw = e.extract_from(text)
        elif e.type == MessageEntityType.TEXT_LINK:
            raw = e.url
        else:
            continue
        # Сохраняем None: распознанная Telegram ссылка, которую мы
        # не смогли безопасно разобрать, не должна исчезать из проверки.
        hosts.append(normalize_host(raw or ""))
    return hosts


# ---------- Команды белого списка (только админы, в группе) ----------
@router.message(F.chat.type.in_(GROUPS), Command("allow", "disallow", "allowed"))
async def whitelist_cmd(message: Message, command: CommandObject, bot: Bot):
    chat_id = message.chat.id
    user = message.from_user
    if not is_anon_admin(message):
        if not user:
            return
        try:
            is_admin = await is_chat_admin(bot, chat_id, user.id, fresh=True)
        except TelegramAPIError as exc:
            logging.warning(
                "Не удалось проверить права для команды whitelist в %s: %s",
                chat_id,
                exc,
            )
            return
        if not is_admin:
            return  # молча игнорируем не-админов

    settings = await get_settings(chat_id)
    if not settings:
        return await message.reply("Чат не зарегистрирован. Добавьте бота заново.")

    if command.command == "allowed":
        items = settings["link_whitelist"]
        return await message.reply(
            "Разрешённые домены:\n" + "\n".join(items) if items else "Белый список пуст."
        )

    host = normalize_host(command.args or "")
    if not host or "." not in host:
        return await message.reply(f"Использование: /{command.command} example.com")

    if command.command == "allow":
        await common.pool.execute(
            """
            UPDATE chat_settings
            SET link_whitelist = CASE WHEN $2 = ANY(link_whitelist)
                THEN link_whitelist ELSE array_append(link_whitelist, $2) END
            WHERE chat_id = $1
            """,
            chat_id, host,
        )
        await message.reply(f"Домен {host} разрешён.")
    else:
        await common.pool.execute(
            "UPDATE chat_settings SET link_whitelist = array_remove(link_whitelist, $2) WHERE chat_id = $1",
            chat_id, host,
        )
        await message.reply(f"Домен {host} убран из белого списка.")


# ---------- Блок ботов ----------
@router.message(F.chat.type.in_(GROUPS), F.new_chat_members)
async def on_new_members(message: Message, bot: Bot):
    chat_id = message.chat.id
    me = await bot.me()
    new_bots = [m for m in message.new_chat_members if m.is_bot and m.id != me.id]
    if not new_bots:
        return

    settings = await get_settings(chat_id)
    if not settings or not settings["block_bots"]:
        return

    # Ботов, добавленных админом, оставляем
    adder = message.from_user
    if is_anon_admin(message) or adder is None:
        # Не применяем автоматический бан, если нельзя установить,
        # кто добавил нового бота.
        return
    if adder:
        try:
            adder_is_admin = await is_chat_admin(
                bot,
                chat_id,
                adder.id,
            )
        except TelegramAPIError as exc:
            # Не баним новых участников, если не удалось достоверно проверить
            # полномочия добавившего их пользователя.
            logging.warning(
                "Не удалось проверить добавившего ботов пользователя в %s: %s",
                chat_id,
                exc,
            )
            return
        if adder_is_admin:
            return

    for b in new_bots:
        try:
            await bot.ban_chat_member(chat_id, b.id)
            await write_log(chat_id, b.id, "bot_banned", f"added_by={adder.id if adder else None}")
            await notify_moderation(
                bot,
                chat_id,
                f"@{b.username}" if b.username else b.full_name,
                "ban",
                "добавление бота не администратором",
            )
        except TelegramAPIError as e:
            logging.warning("Не удалось забанить бота %s в %s: %s", b.id, chat_id, e)
    try:
        await message.delete()
    except TelegramAPIError:
        pass


# ---------- Блок ссылок ----------
@router.message(F.chat.type.in_(GROUPS), F.text | F.caption)
async def on_message(message: Message, bot: Bot):
    # Анонимные админы, каналы и боты не трогаем
    if message.sender_chat or message.is_automatic_forward:
        return
    user = message.from_user
    if user is None or user.is_bot:
        return

    hosts = find_hosts(message)
    if not hosts:
        return  # Telegram не выделил в сообщении URL/TEXT_LINK entity

    # Неизвестную/неразбираемую ссылку удаляем, но не наказываем автора
    # только за невозможность безопасно классифицировать URL.
    unparseable_link = any(host is None for host in hosts)
    valid_hosts = [host for host in hosts if host is not None]

    chat_id = message.chat.id
    settings = await get_settings(chat_id)
    if not settings or not settings["block_links"]:
        return

    try:
        # Для потока обычных сообщений используем короткий кэш:
        # это снижает число запросов Telegram API при сериях ссылок.
        # Команды изменения whitelist по-прежнему проверяют права fresh=True.
        is_admin = await is_chat_admin(
            bot,
            chat_id,
            user.id,
        )
    except TelegramAPIError as exc:
        # Ошибка проверки прав не должна превращаться в бан/мут.
        logging.warning(
            "Не удалось проверить права автора ссылки в %s; сообщение не наказываем: %s",
            chat_id,
            exc,
        )
        return
    if is_admin:
        return

    whitelist = settings["link_whitelist"]
    bad = [h for h in valid_hosts if not is_allowed(h, whitelist)]
    if not bad and not unparseable_link:
        return

    try:
        await message.delete()
    except TelegramAPIError as e:
        logging.warning("Нет прав удалить сообщение в %s: %s", chat_id, e)
        return

    punishment_succeeded = settings["punishment"] == "delete"
    if bad:
        try:
            await apply_punishment(bot, chat_id, user.id, settings["punishment"])
            punishment_succeeded = True
        except TelegramAPIError as e:
            logging.warning("Не удалось наказать %s в %s: %s", user.id, chat_id, e)

    details = [str(host) for host in bad]
    if unparseable_link:
        details.append("[ссылка удалена: домен не удалось безопасно проверить]")
    details_text = ", ".join(details)
    await write_log(chat_id, user.id, "link_blocked", details_text)
    if punishment_succeeded:
        await notify_moderation(
            bot,
            chat_id,
            f"@{user.username}" if user.username else user.full_name,
            settings["punishment"] if bad else "delete",
            "запрещённая ссылка" if bad else "не удалось безопасно проверить ссылку",
            details_text,
        )
