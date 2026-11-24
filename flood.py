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
                  AND created_at <= clock_timestamp() - ($3 * interval '1 second')
                """,
                chat_id, user_id, window,
            )
            await conn.execute(
                """
                INSERT INTO flood_events (chat_id, user_id, event_key, created_at)
                VALUES ($1, $2, $3, clock_timestamp())
                ON CONFLICT (chat_id, user_id, event_key) DO NOTHING
                """,
                chat_id, user_id, key,
            )
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM flood_events
                WHERE chat_id = $1 AND user_id = $2
                  AND created_at > clock_timestamp() - ($3 * interval '1 second')
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


# Local, always-on guard. The bot enforces a single polling process per token,
# so this limiter is shared by every update handled by that process and does not
# depend on PostgreSQL availability or a per-chat toggle.
from collections import OrderedDict, deque
import time


class LocalFloodGuard:
    def __init__(self, max_users: int = 20_000):
        self.max_users = max_users
        self._users = OrderedDict()
        self._lock = asyncio.Lock()

    async def record(
        self, *, chat_id: int, user_id: int, message_id: int,
        media_group_id: str | None, limit: int = 5, window: int = 5,
    ) -> tuple[int, bool, bool]:
        if limit < 1 or window < 1:
            raise ValueError("flood limit and window must be positive")
        now = time.monotonic()
        key = (chat_id, user_id)
        event_key = _event_key(message_id, media_group_id)

        async with self._lock:
            state = self._users.get(key)
            if state is None:
                state = {"events": deque(), "violations": deque(), "muted": False}
                self._users[key] = state
            else:
                self._users.move_to_end(key)

            events = state["events"]
            while events and events[0][0] <= now - window:
                events.popleft()

            # Telegram can redeliver an update; album parts are one event.
            if any(existing_key == event_key for _, existing_key in events):
                return len(events), False, False

            events.append((now, event_key))
            count = len(events)
            exceeded = count > limit

            violations = state["violations"]
            while violations and violations[0] <= now - window:
                violations.popleft()
            if exceeded:
                violations.append(now)

            # Delete every over-limit message. Mute only after three distinct
            # over-limit messages in the same rolling window to avoid punishing
            # a single accidental burst.
            should_mute = exceeded and len(violations) >= 3 and not state["muted"]
            if should_mute:
                state["muted"] = True

            if not events and not violations:
                self._users.pop(key, None)
            while len(self._users) > self.max_users:
                self._users.popitem(last=False)

            return count, exceeded, should_mute


local_flood_guard = LocalFloodGuard()
