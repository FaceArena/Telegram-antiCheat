import asyncio
import logging
import time

import asyncpg
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

pool: asyncpg.Pool  # выставляется в bot.py при старте

logger = logging.getLogger(__name__)

# Снимок списка администраторов кэшируется на короткое время на чат,
# чтобы поток сообщений от множества разных пользователей не создавал
# отдельный getChatMember API-запрос для каждого автора.
ADMIN_CACHE_TTL = 5  # секунд
ADMIN_ERROR_TTL = 2  # короткий backoff при сбоях Telegram API
ADMIN_CACHE_MAX_ENTRIES = 10_000
_admin_cache: dict[int, tuple[float, frozenset[int]]] = {}
_admin_error_cache: dict[int, tuple[float, TelegramAPIError]] = {}
_admin_cache_locks = tuple(asyncio.Lock() for _ in range(64))


def _store_admin_error(chat_id: int, error: TelegramAPIError) -> None:
    now = time.monotonic()
    if len(_admin_error_cache) >= ADMIN_CACHE_MAX_ENTRIES and chat_id not in _admin_error_cache:
        expired = [
            cached_chat_id
            for cached_chat_id, (expires_at, _) in _admin_error_cache.items()
            if now >= expires_at
        ]
        for cached_chat_id in expired:
            _admin_error_cache.pop(cached_chat_id, None)
        while len(_admin_error_cache) >= ADMIN_CACHE_MAX_ENTRIES:
            _admin_error_cache.pop(next(iter(_admin_error_cache)), None)
    _admin_error_cache[chat_id] = (now + ADMIN_ERROR_TTL, error)


def _store_admin_cache(chat_id: int, admin_ids: set[int]) -> None:
    now = time.monotonic()
    if len(_admin_cache) >= ADMIN_CACHE_MAX_ENTRIES and chat_id not in _admin_cache:
        expired = [
            cached_chat_id
            for cached_chat_id, (cached_at, _) in _admin_cache.items()
            if now - cached_at >= ADMIN_CACHE_TTL
        ]
        for cached_chat_id in expired:
            _admin_cache.pop(cached_chat_id, None)

        while len(_admin_cache) >= ADMIN_CACHE_MAX_ENTRIES:
            oldest_chat_id = next(iter(_admin_cache))
            _admin_cache.pop(oldest_chat_id, None)

    _admin_error_cache.pop(chat_id, None)
    _admin_cache[chat_id] = (now, frozenset(admin_ids))


async def invalidate_admin_cache(chat_id: int, user_id: int) -> None:
    """Сбрасывает снимок и backoff ошибок после изменения статуса участника."""
    lock = _admin_cache_locks[chat_id % len(_admin_cache_locks)]
    async with lock:
        _admin_cache.pop(chat_id, None)
        _admin_error_cache.pop(chat_id, None)


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int, fresh: bool = False) -> bool:
    """Проверяет статус администратора.

    Обычные проверки используют короткоживущий снимок администраторов чата.
    fresh=True обращается напрямую к getChatMember для критичных действий.
    Ошибки Telegram API не превращаются в отрицательный результат; повторные
    сбои кратко ограничиваются backoff-кэшем и сбрасываются при обновлении статуса.
    """
    if fresh:
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
        return member.status in (
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        )

    now = time.monotonic()
    error_hit = _admin_error_cache.get(chat_id)
    if error_hit:
        expires_at, cached_error = error_hit
        if now < expires_at:
            raise cached_error
        _admin_error_cache.pop(chat_id, None)

    hit = _admin_cache.get(chat_id)
    if hit:
        cached_at, admin_ids = hit
        if now - cached_at < ADMIN_CACHE_TTL:
            return user_id in admin_ids
        _admin_cache.pop(chat_id, None)

    # Один запрос getChatAdministrators на чат при одновременном потоке сообщений.
    # Фиксированное число lock-shard не позволяет накапливать lock
    # для каждого когда-либо встреченного chat_id.
    lock = _admin_cache_locks[chat_id % len(_admin_cache_locks)]

    async with lock:
        # Другой обработчик мог заполнить кэш, пока мы ждали lock.
        now = time.monotonic()
        error_hit = _admin_error_cache.get(chat_id)
        if error_hit:
            expires_at, cached_error = error_hit
            if now < expires_at:
                raise cached_error
            _admin_error_cache.pop(chat_id, None)

        hit = _admin_cache.get(chat_id)
        if hit:
            cached_at, admin_ids = hit
            if now - cached_at < ADMIN_CACHE_TTL:
                return user_id in admin_ids
            _admin_cache.pop(chat_id, None)

        try:
            members = await bot.get_chat_administrators(chat_id)
        except TelegramAPIError as error:
            _store_admin_error(chat_id, error)
            logger.warning(
                "Telegram API failed while loading chat administrators "
                "(chat_id=%s); short backoff enabled",
                chat_id,
                exc_info=True,
            )
            raise

        admin_ids = {member.user.id for member in members}
        _store_admin_cache(chat_id, admin_ids)
        return user_id in admin_ids
