"""Staging-only PostgreSQL load probe for the bot's per-message settings lookup.

This does not call Telegram and is not an end-to-end bot capacity test.
"""
import argparse
import asyncio
import os
import time

import asyncpg


QUERY = """
SELECT s.chat_id
FROM chat_settings AS s
JOIN chats AS c USING (chat_id)
WHERE s.chat_id = $1 AND c.active
"""


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


async def run_load(
    database_url: str,
    chat_id: int,
    requests: int,
    concurrency: int,
    pool_size: int,
) -> tuple[list[float], int]:
    pool = await asyncpg.create_pool(
        database_url,
        min_size=1,
        max_size=pool_size,
        command_timeout=30,
    )
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors = 0

    async def one_request() -> None:
        nonlocal errors
        async with semaphore:
            started = time.perf_counter()
            try:
                async with pool.acquire() as connection:
                    found = await connection.fetchval(QUERY, chat_id)
                if found is None:
                    raise RuntimeError(
                        "LOAD_TEST_CHAT_ID is not an active chat in chat_settings."
                    )
                latencies.append((time.perf_counter() - started) * 1000)
            except Exception:
                errors += 1

    started = time.perf_counter()
    try:
        await asyncio.gather(*(one_request() for _ in range(requests)))
    finally:
        elapsed = time.perf_counter() - started
        await pool.close()

    throughput = requests / elapsed if elapsed else 0.0
    print(f"Requests: {requests}")
    print(f"Concurrency cap: {concurrency}")
    print(f"PostgreSQL pool size: {pool_size}")
    print(f"Elapsed: {elapsed:.2f}s")
    print(f"Throughput: {throughput:.1f} requests/s")
    print(f"Latency p50/p95/p99: {percentile(latencies, .50):.1f}/"
          f"{percentile(latencies, .95):.1f}/{percentile(latencies, .99):.1f} ms")
    print(f"Errors: {errors}")
    return latencies, errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=10_000)
    parser.add_argument("--concurrency", type=int, default=1_000)
    parser.add_argument("--pool-size", type=int, default=10)
    args = parser.parse_args()

    if args.requests < 1 or args.concurrency < 1 or args.pool_size < 1:
        parser.error("requests, concurrency and pool-size must be positive")

    database_url = os.getenv("DATABASE_URL")
    chat_id = os.getenv("LOAD_TEST_CHAT_ID")
    if not database_url or not chat_id:
        parser.error("Set DATABASE_URL and LOAD_TEST_CHAT_ID for a staging database")

    _, errors = asyncio.run(
        run_load(
            database_url,
            int(chat_id),
            args.requests,
            args.concurrency,
            args.pool_size,
        )
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
