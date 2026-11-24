import asyncio
import logging

FLOOD_EVENT_RETENTION_SECONDS = 120
FLOOD_CLEANUP_INTERVAL_SECONDS = 300


def _event_key(message_id: int, media_group_id: str | None) -> str:
    # Повторная доставка update идемпотентна; весь альбом — одно событие.
    return f"album:{media_group_id}" if media_group_id else f"message:{message_id}"


async def record_flood_message(
    pool, *, chat_id: int, user_id: int, message_id: int,
    media_group_id: str | None, window: int,
) -> int:
    """Атомарно регистрирует сообщение и возвращает число событий в окне."""
    if not 3 <= window <= 60:
        raise ValueError("flood window must be between 3 and 60 seconds")

    key = _event_key(message_id, media_group_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended('flood:' || $1::text || ':' || $2::text, 0)
                )
                """,
                chat_id, user_id,
            )
            await conn.execute(
                """
                DELETE FROM flood_events
                WHERE chat_id = $1 AND user_id = $2
                  AND created_at <= now() - ($3 * interval '1 second')
                """,
                chat_id, user_id, window,
            )
            await conn.execute(
                """
                INSERT INTO flood_events (chat_id, user_id, event_key)
                VALUES ($1, $2, $3)
                ON CONFLICT (chat_id, user_id, event_key) DO NOTHING
                """,
                chat_id, user_id, key,
            )
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM flood_events
                WHERE chat_id = $1 AND user_id = $2
                  AND created_at > now() - ($3 * interval '1 second')
                """,
                chat_id, user_id, window,
            )
    return int(count)


async def cleanup_flood_events(pool) -> int:
    result = await pool.execute(
        """
        DELETE FROM flood_events
        WHERE created_at < now() - ($1 * interval '1 second')
        """,
        FLOOD_EVENT_RETENTION_SECONDS,
    )
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (AttributeError, ValueError):
        return 0


async def flood_cleanup_worker(pool) -> None:
    while True:
        try:
            deleted = await cleanup_flood_events(pool)
            if deleted:
                logging.info("Антифлуд: очищено %s устаревших событий.", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка очистки событий антифлуда")
        await asyncio.sleep(FLOOD_CLEANUP_INTERVAL_SECONDS)
