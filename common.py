import logging
import time

import asyncpg
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

pool: asyncpg.Pool  # выставляется в bot.py при старте

logger = logging.getLogger(__name__)

# Короткий TTL уменьшает окно устаревших прав администратора.
ADMIN_CACHE_TTL = 5  # секунд
_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}


def invalidate_admin_cache(chat_id: int, user_id: int) -> None:
    """Сбрасывает кэш прав после Telegram chat_member update."""
    _admin_cache.pop((chat_id, user_id), None)


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int, fresh: bool = False) -> bool:
    """Проверяет права через Telegram.

    При ошибке Telegram API исключение передаётся вызывающему коду:
    ошибка проверки не должна трактоваться как подтверждение, что пользователь
    не администратор. Ошибочные результаты никогда не кэшируются.
    fresh=True полностью обходит кэш.
    """
    key = (chat_id, user_id)
    now = time.monotonic()

    if fresh:
        _admin_cache.pop(key, None)
    else:
        hit = _admin_cache.get(key)
        if hit:
            cached_at, cached_result = hit
            if now - cached_at < ADMIN_CACHE_TTL:
                return cached_result
            _admin_cache.pop(key, None)

    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        logger.warning(
            "Telegram API failed while checking admin status "
            "(chat_id=%s, user_id=%s); result was not cached",
            chat_id,
            user_id,
            exc_info=True,
        )
        raise

    is_admin = member.status in (
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
    )
    _admin_cache[key] = (time.monotonic(), is_admin)
    return is_admin
