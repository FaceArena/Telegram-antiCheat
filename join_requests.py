import asyncio
import logging

logger = logging.getLogger(__name__)

MIN_REQUEST_LIMIT = 3
MAX_REQUEST_LIMIT = 30
MIN_REQUEST_WINDOW = 5
MAX_REQUEST_WINDOW = 120
MIN_LOCKDOWN_SECONDS = 60
MAX_LOCKDOWN_SECONDS = 3600
EVENT_RETENTION_SECONDS = 3600
CLEANUP_INTERVAL_SECONDS = 300


def validate_request_config(limit: int, window_seconds: int, lockdown_seconds: int) -> None:
    if not MIN_REQUEST_LIMIT <= limit <= MAX_REQUEST_LIMIT:
        raise ValueError("request_limit must be between 3 and 30")
    if not MIN_REQUEST_WINDOW <= window_seconds <= MAX_REQUEST_WINDOW:
        raise ValueError("request_window must be between 5 and 120 seconds")
    if not MIN_LOCKDOWN_SECONDS <= lockdown_seconds <= MAX_LOCKDOWN_SECONDS:
        raise ValueError("request_lockdown_seconds must be between 60 and 3600 seconds")


async def register_request(
    pool,
    chat_id: int,
    user_id: int,
    limit: int,
    window_seconds: int,
    lockdown_seconds: int,
) -> tuple[int, bool, bool]:
    """Count unique join requests and atomically activate a temporary channel lockdown.

    Returns (unique_requests_in_window, lockdown_active, newly_triggered).
    The triggering request is included in the count and must be declined by the caller.
    """
    validate_request_config(limit, window_seconds, lockdown_seconds)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended('kimguard:join-request:' || $1::text, 0)
                )
                """,
                str(chat_id),
            )
            await conn.execute(
                """
                DELETE FROM channel_request_events
                WHERE chat_id = $1
                  AND requested_at < now() - make_interval(secs => $2)
                """,
                chat_id,
                float(window_seconds),
            )
            await conn.execute(
                """
                INSERT INTO channel_request_events (chat_id, user_id, requested_at)
                VALUES ($1, $2, now())
                ON CONFLICT (chat_id, user_id)
                DO UPDATE SET requested_at = EXCLUDED.requested_at
                """,
                chat_id,
                user_id,
            )
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM channel_request_events
                WHERE chat_id = $1
                  AND requested_at >= now() - make_interval(secs => $2)
                """,
                chat_id,
                float(window_seconds),
            )
            active = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM channel_request_state
                    WHERE chat_id = $1 AND locked_until > now()
                )
                """,
                chat_id,
            )
            triggered = False
            if count >= limit and not active:
                await conn.execute(
                    """
                    INSERT INTO channel_request_state (
                        chat_id, locked_until, triggered_at, request_count
                    )
                    VALUES (
                        $1,
                        now() + make_interval(secs => $2),
                        now(),
                        $3
                    )
                    ON CONFLICT (chat_id) DO UPDATE
                    SET locked_until = EXCLUDED.locked_until,
                        triggered_at = EXCLUDED.triggered_at,
                        request_count = EXCLUDED.request_count
                    """,
                    chat_id,
                    float(lockdown_seconds),
                    int(count),
                )
                active = True
                triggered = True
    return int(count), bool(active), triggered


async def request_cleanup_worker(pool) -> None:
    """Keep request event storage bounded; expired lockouts are not active."""
    while True:
        try:
            await pool.execute(
                """
                DELETE FROM channel_request_events
                WHERE requested_at < now() - make_interval(secs => $1)
                """,
                float(EVENT_RETENTION_SECONDS),
            )
            await pool.execute(
                """
                DELETE FROM channel_request_state
                WHERE locked_until < now() - interval '1 day'
                """
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Channel join-request cleanup worker failed")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


def decide_join_request_action(protection_enabled: bool, lockdown_active: bool, auto_accept_enabled: bool) -> str:
    """Return decline, approve, or pending; active anti-raid lockdown always wins."""
    if protection_enabled and lockdown_active:
        return "decline"
    if auto_accept_enabled:
        return "approve"
    return "pending"


async def evaluate_join_request(
    pool,
    chat_id: int,
    user_id: int,
    protection_enabled: bool,
    auto_accept_enabled: bool,
    limit: int,
    window_seconds: int,
    lockdown_seconds: int,
) -> tuple[str, int, bool, bool]:
    """Resolve a request; anti-raid tracking is independent from auto-approval.

    Returns (action, count, lockdown_active, newly_triggered). If anti-raid is
    disabled, no request counters or stale lockout state are consulted.
    """
    count, locked, triggered = 0, False, False
    if protection_enabled:
        count, locked, triggered = await register_request(
            pool, chat_id, user_id, limit, window_seconds, lockdown_seconds
        )
    action = decide_join_request_action(
        protection_enabled, locked, auto_accept_enabled
    )
    return action, count, locked, triggered
