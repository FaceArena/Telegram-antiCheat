import time

import asyncpg
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

pool: asyncpg.Pool  # выставляется в bot.py при старте

ADMIN_CACHE_TTL = 300  # секунд
_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int, fresh: bool = False) -> bool:
    """Проверка прав через Telegram. fresh=True игнорирует кеш (для опасных действий)."""
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < ADMIN_CACHE_TTL:
        return hit[1]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        ok = member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except TelegramAPIError:
        ok = False
    _admin_cache[key] = (time.monotonic(), ok)
    return ok
