import asyncio
import logging

FLOOD_EVENT_RETENTION_SECONDS = 120
FLOOD_CLEANUP_INTERVAL_SECONDS = 300

# The always-on baseline may only be tightened from the private settings panel.
MIN_FLOOD_LIMIT = 2
MAX_FLOOD_LIMIT = 5
MIN_FLOOD_WINDOW = 3
MAX_FLOOD_WINDOW = 5


def validate_flood_settings(limit: int, window: int) -> None:
    """Reject settings that would weaken or misrepresent the hard baseline."""
    if not MIN_FLOOD_LIMIT <= limit <= MAX_FLOOD_LIMIT:
        raise ValueError(f"flood limit must be {MIN_FLOOD_LIMIT}..{MAX_FLOOD_LIMIT}")
    if not MIN_FLOOD_WINDOW <= window <= MAX_FLOOD_WINDOW:
        raise ValueError(f"flood window must be {MIN_FLOOD_WINDOW}..{MAX_FLOOD_WINDOW}")


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
    """Single-process rolling-window spam detector with one-shot escalation."""

    def __init__(self, max_users: int = 20_000):
        self.max_users = max_users
        self._users = OrderedDict()
        self._lock = asyncio.Lock()

    async def record(
        self, *, chat_id: int, user_id: int, message_id: int,
        media_group_id: str | None, limit: int = 5, window: int = 5,
        fingerprint: str | None = None,
    ) -> tuple[int, bool, str | None]:
        if limit < 1 or window < 1:
            raise ValueError("flood limit and window must be positive")
        now = time.monotonic()
        key = (chat_id, user_id)
        event_key = _event_key(message_id, media_group_id)

        async with self._lock:
            state = self._users.get(key)
            if state is None:
                state = {"events": deque(), "action": None}
                self._users[key] = state
            else:
                self._users.move_to_end(key)

            events = state["events"]
            while events and events[0][0] <= now - max(window, 10):
                events.popleft()
            if not events:
                # A new incident after the old rolling window can be punished
                # independently (including after a previous mute expires).
                state["action"] = None

            if any(existing_key == event_key for _, existing_key, _, _ in events):
                return len([e for e in events if e[0] > now - window]), False, None

            events.append((now, event_key, message_id, fingerprint))
            recent = [e for e in events if e[0] > now - window]
            count = len(recent)
            exceeded_rate = count > limit

            repeated_count = 0
            if fingerprint:
                repeated_count = sum(
                    1 for ts, _, _, fp in events
                    if ts > now - 10 and fp == fingerprint
                )
            repeated_spam = repeated_count >= 3
            exceeded = exceeded_rate or repeated_spam

            # Escalate immediately: normal burst -> 1h mute; extreme burst
            # (10+ messages in the configured window) -> ban.
            requested_action = "ban" if count >= 10 else ("mute" if exceeded else None)
            previous_action = state["action"]
            action = None
            if requested_action == "ban" and previous_action != "ban":
                action = "ban"
                state["action"] = "ban"
            elif requested_action == "mute" and previous_action is None:
                action = "mute"
                state["action"] = "mute"

            while len(self._users) > self.max_users:
                self._users.popitem(last=False)

            return count, exceeded, action

    async def recent_message_ids(self, chat_id: int, user_id: int) -> list[int]:
        async with self._lock:
            state = self._users.get((chat_id, user_id))
            if state is None:
                return []
            return list(dict.fromkeys(message_id for _, _, message_id, _ in state["events"]))

    async def punishment_failed(self, chat_id: int, user_id: int, punishment: str) -> None:
        async with self._lock:
            state = self._users.get((chat_id, user_id))
            if state is None:
                return
            if state["action"] == punishment:
                state["action"] = None


local_flood_guard = LocalFloodGuard()
