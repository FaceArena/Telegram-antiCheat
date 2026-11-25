import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, MessageEntityType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
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


async def write_log(
    chat_id: int,
    user_id: int | None,
    action: str,
    details: str = "",
    *,
    actor_user_id: int | None = None,
):
    """Persist an audit event. user_id is the affected target; actor is optional."""
    try:
        await common.pool.execute(
            """
            INSERT INTO logs (
                chat_id, user_id, action, details, actor_user_id, target_user_id
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            chat_id,
            user_id if user_id is not None else actor_user_id,
            action,
            details[:500],
            actor_user_id,
            user_id,
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


def format_moderation_notice(
    actor_name: str,
    punishment: str,
    reason: str,
    details: str = "",
) -> str:
    """Build a short, bounded plain-text notice for the group."""
    action = {
        "ban": "заблокирован",
        "mute": "ограничен",
        "delete": "сообщение удалено",
    }.get(punishment, "обработан")
    actor = " ".join(str(actor_name).split())[:80] or "пользователь"
    reason_text = " ".join(str(reason).split())[:120] or "нарушение правил"
    details_text = " ".join(str(details).split())[:240]
    suffix = f" ({details_text})" if details_text else ""
    return f"🛡 KimGuard: {actor} — {action}. Причина: {reason_text}{suffix}."


async def notify_moderation(
    bot: Bot,
    chat_id: int,
    actor_name: str,
    punishment: str,
    reason: str,
    details: str = "",
) -> None:
    """Post a compact group notice after a moderation action actually succeeds."""
    try:
        await common.send_temporary_message(
            bot,
            chat_id,
            format_moderation_notice(actor_name, punishment, reason, details),
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


# ---------- Блок ссылок и упоминаний ----------
_CHANNEL_MENTION_CACHE: dict[str, tuple[float, bool]] = {}
_CHANNEL_MENTION_CACHE_TTL = 300

async def find_channel_mentions(message: Message, bot: Bot) -> list[str]:
    """Отличает публичные каналы от обычных @username через Telegram getChat."""
    channels = []
    now = time.monotonic()
    for mention in find_mentions(message):
        key = mention.casefold()
        cached = _CHANNEL_MENTION_CACHE.get(key)
        if cached and cached[0] > now:
            is_channel = cached[1]
        else:
            try:
                chat = await bot.get_chat(mention)
            except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError):
                # A temporary Telegram failure is not evidence that the
                # username is a person. Fail closed for this message, but do
                # not cache the uncertain result.
                logging.warning(
                    "Не удалось проверить @mention из-за временной ошибки Telegram; "
                    "считаем упоминание потенциально каналом: %s",
                    mention,
                    exc_info=True,
                )
                channels.append(mention)
                continue
            except TelegramAPIError:
                # Invalid/nonexistent usernames are not treated as channels.
                is_channel = False
            else:
                is_channel = chat.type == ChatType.CHANNEL
            _CHANNEL_MENTION_CACHE[key] = (
                now + (_CHANNEL_MENTION_CACHE_TTL if is_channel else 30),
                is_channel,
            )
        if is_channel:
            channels.append(mention)
    if len(_CHANNEL_MENTION_CACHE) > 2048:
        expired = [name for name, (expires, _) in _CHANNEL_MENTION_CACHE.items() if expires <= now]
        for name in expired:
            _CHANNEL_MENTION_CACHE.pop(name, None)
        if len(_CHANNEL_MENTION_CACHE) > 2048:
            overflow = len(_CHANNEL_MENTION_CACHE) - 2048
            for name in list(_CHANNEL_MENTION_CACHE)[:overflow]:
                _CHANNEL_MENTION_CACHE.pop(name, None)
    return channels

def find_mentions(message: Message) -> list[str]:
    """Извлекает Telegram-распознанные @username-упоминания."""
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    mentions = []
    for entity in entities:
        if entity.type == MessageEntityType.MENTION:
            value = entity.extract_from(text)
            if value:
                mentions.append(value)
    return mentions


def find_bot_username_mentions(message: Message) -> list[str]:
    """Находит явные @username-упоминания, чей username заканчивается на bot."""
    return [
        mention
        for mention in find_mentions(message)
        if mention.removeprefix("@").casefold().endswith("bot")
    ]


@router.edited_message(F.chat.type.in_(GROUPS), F.text | F.caption)
@router.message(F.chat.type.in_(GROUPS), F.text | F.caption)
async def on_message(message: Message, bot: Bot):
    # Анонимные админы, каналы и боты не трогаем.
    if message.sender_chat or message.is_automatic_forward:
        return
    user = message.from_user
    if user is None or user.is_bot:
        return

    chat_id = message.chat.id
    settings = await get_settings(chat_id)
    if not settings or not (
        settings["block_links"]
        or settings["block_mentions"]
        or settings["block_bot_mentions"]
    ):
        return

    try:
        is_admin = await is_chat_admin(bot, chat_id, user.id)
    except TelegramAPIError as exc:
        # Не удаляем сообщение, пока Telegram не подтвердил статус автора:
        # иначе можно ошибочно удалить сообщение администратора.
        logging.warning(
            "Не удалось проверить права автора сообщения в %s; фильтр ссылок/упоминаний пропущен: %s",
            chat_id,
            exc,
        )
        return
    if is_admin:
        return

    raw_link_hosts = find_hosts(message) if settings["block_links"] else []
    whitelist = settings.get("link_whitelist") or []
    blocked_links = [
        host for host in raw_link_hosts
        if not is_allowed(host, whitelist)
    ]
    blocked_mentions = await find_channel_mentions(message, bot) if settings["block_mentions"] else []
    blocked_bot_mentions = (
        find_bot_username_mentions(message) if settings["block_bot_mentions"] else []
    )

    # Реклама username ботов — отдельное правило: баним именно автора сообщения,
    # независимо от общего наказания группы. Сам упомянутый бот не затрагивается.
    if blocked_bot_mentions:
        try:
            await message.delete()
        except TelegramAPIError as exc:
            logging.warning(
                "Не удалось удалить рекламу бота в %s: %s",
                chat_id,
                exc,
            )
        try:
            await apply_punishment(bot, chat_id, user.id, "ban")
        except TelegramAPIError as exc:
            logging.warning(
                "Не удалось забанить автора рекламы бота %s в %s: %s",
                user.id,
                chat_id,
                exc,
            )
            await write_log(
                chat_id, user.id, "bot_advertising_ban_failed",
                ", ".join(blocked_bot_mentions),
            )
        else:
            details = ", ".join(blocked_bot_mentions)[:500]
            await write_log(chat_id, user.id, "bot_advertising", details)
            await notify_moderation(
                bot,
                chat_id,
                f"@{user.username}" if user.username else user.full_name,
                "ban",
                "реклама ботов",
                details,
            )
        return

    if not blocked_links and not blocked_mentions:
        return

    # Любая URL/TEXT_LINK entity блокируется, whitelist не применяется.
    # Неразбираемые URL-entities также считаются нарушением.
    try:
        await message.delete()
    except TelegramAPIError as exc:
        logging.warning(
            "Нет прав удалить ссылку/упоминание в %s: %s",
            chat_id,
            exc,
        )
        return

    reason_parts = []
    if blocked_links:
        reason_parts.append("ссылки")
    if blocked_mentions:
        reason_parts.append("упоминания")
    details_parts = [str(host) for host in blocked_links]
    details_parts.extend(blocked_mentions)
    details_text = ", ".join(details_parts)[:500]
    reason = "link_blocked" if blocked_links else "mention_blocked"

    try:
        await apply_punishment(bot, chat_id, user.id, settings["punishment"])
        punishment_succeeded = True
    except TelegramAPIError as exc:
        punishment_succeeded = False
        logging.warning(
            "Не удалось применить наказание к %s в %s: %s",
            user.id,
            chat_id,
            exc,
        )

    await write_log(chat_id, user.id, reason, details_text)
    if punishment_succeeded:
        await notify_moderation(
            bot,
            chat_id,
            f"@{user.username}" if user.username else user.full_name,
            settings["punishment"],
            "ссылки и упоминания запрещены",
            ", ".join(reason_parts),
        )
