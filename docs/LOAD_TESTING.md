# Staging load test

The repository currently limits aiogram update concurrency to 50 and the PostgreSQL pool to 10 connections. A count of 10,000 group members is not the same as 10,000 simultaneous updates; capacity must be measured under realistic traffic.

## Database lookup probe

Run this only against a disposable/staging Supabase project, never production:

```bash
export DATABASE_URL='postgresql://...'
export LOAD_TEST_CHAT_ID='-100...'
python scripts/load_test_db.py --requests 10000 --concurrency 1000 --pool-size 10
```

The script simulates concurrent executions of the active-chat settings lookup used by message handlers. It reports throughput and p50/p95/p99 latency. It does **not** call Telegram, exercise message moderation, test Telegram rate limits, or prove support for 10,000 simultaneous Telegram updates.

Before enabling a public bot, run this probe against staging, then separately test Telegram end-to-end with controlled groups and gradual concurrency. Keep one Railway replica because the bot uses a PostgreSQL session advisory lock to enforce a single long-polling process.

## Operational defaults

- Audit records older than 90 days are removed hourly in batches of at most 5,000 rows (up to 20 batches per run).
- Bot blocking defaults to OFF for newly registered groups. Existing groups keep their stored setting; no existing preference is rewritten.
- CAPTCHA timeout does not auto-ban or auto-unban: Telegram does not expose who established a ban, so a timed-out member is recorded for administrator review instead of risking reversal of a manual admin ban.
