import asyncio
import logging

logger = logging.getLogger(__name__)

EVENT_RETENTION_SECONDS = 3600
CLEANUP_INTERVAL_SECONDS = 300


def validate_raid_config(join_limit: int, window_seconds: int, lockdown_seconds: int) -> None:
    if not 3 <= join_limit <= 30:
        raise ValueError("raid_join_limit must be between 3 and 30")
    if not 5 <= window_seconds <= 120:
        raise ValueError("raid_window must be between 5 and 120 seconds")
    if not 60 <= lockdown_seconds <= 3600:
        raise ValueError("raid_lockdown_seconds must be between 60 and 3600 seconds")


def threshold_reached(join_count: int, limit: int) -> bool:
    """Return True only when the configured unique-join threshold is met."""
    return join_count >= limit


async def is_active(pool, chat_id: int) -> bool:
    return bool(
        await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM raid_state
                WHERE chat_id = $1 AND locked_until > now()
            )
            """,
            chat_id,
        )
    )


async def register_join(
    pool,
    chat_id: int,
    user_id: int,
    join_limit: int,
    window_seconds: int,
    lockdown_seconds: int,
) -> tuple[int, bool, bool]:
    """Record one unique join and atomically activate a temporary raid mode.

    Returns (unique_joins_in_window, raid_mode_active, newly_triggered).
    The per-chat transaction advisory lock prevents concurrent join updates
    from racing past the threshold or creating duplicate raid notifications.
    """
    validate_raid_config(join_limit, window_seconds, lockdown_seconds)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended('telegram-security-ai:raid:' || $1::text, 0)
                )
                """,
                chat_id,
            )
            await conn.execute(
                """
                DELETE FROM raid_events
                WHERE chat_id = $1
                  AND joined_at < now() - make_interval(secs => $2)
                """,
                chat_id,
                float(window_seconds),
            )
            await conn.execute(
                """
                INSERT INTO raid_events (chat_id, user_id, joined_at)
                VALUES ($1, $2, now())
                ON CONFLICT (chat_id, user_id)
                DO UPDATE SET joined_at = EXCLUDED.joined_at
                """,
                chat_id,
                user_id,
            )
            join_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM raid_events
                WHERE chat_id = $1
                  AND joined_at >= now() - make_interval(secs => $2)
                """,
                chat_id,
                float(window_seconds),
            )
            active = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM raid_state
                    WHERE chat_id = $1 AND locked_until > now()
                )
                """,
                chat_id,
            )
            triggered = False
            if threshold_reached(join_count, join_limit) and not active:
                await conn.execute(
                    """
                    INSERT INTO raid_state (
                        chat_id, locked_until, triggered_at, join_count
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
                        join_count = EXCLUDED.join_count
                    """,
                    chat_id,
                    float(lockdown_seconds),
                    join_count,
                )
                active = True
                triggered = True

    return int(join_count), bool(active), triggered


async def anti_raid_worker(pool) -> None:
    """Bound persistent event storage; expired raid states are not active."""
    while True:
        try:
            await pool.execute(
                """
                DELETE FROM raid_events
                WHERE joined_at < now() - make_interval(secs => $1)
                """,
                float(EVENT_RETENTION_SECONDS),
            )
            await pool.execute(
                """
                DELETE FROM raid_state
                WHERE locked_until < now() - interval '1 day'
                """
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Anti-raid cleanup worker failed")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
