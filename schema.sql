CREATE TABLE IF NOT EXISTS chats (
    chat_id      BIGINT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    chat_type    TEXT NOT NULL,
    added_by     BIGINT,
    bot_is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id         BIGINT PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
    block_bots      BOOLEAN NOT NULL DEFAULT FALSE,
    block_links     BOOLEAN NOT NULL DEFAULT FALSE,
    block_mentions  BOOLEAN NOT NULL DEFAULT FALSE,
    block_bot_mentions BOOLEAN NOT NULL DEFAULT FALSE,
    captcha         BOOLEAN NOT NULL DEFAULT FALSE,
    punishment      TEXT NOT NULL DEFAULT 'delete'
                    CHECK (punishment IN ('delete', 'mute', 'ban')),
    link_whitelist  TEXT[] NOT NULL DEFAULT '{}',
    log_chat_id     BIGINT,
    antiflood       BOOLEAN NOT NULL DEFAULT FALSE,
    stopwords       BOOLEAN NOT NULL DEFAULT FALSE,
    stopword_list   TEXT[] NOT NULL DEFAULT '{}',
    flood_limit     INTEGER NOT NULL DEFAULT 5
                    CHECK (flood_limit BETWEEN 2 AND 5),
    flood_window    INTEGER NOT NULL DEFAULT 5
                    CHECK (flood_window BETWEEN 3 AND 5),
    anti_raid       BOOLEAN NOT NULL DEFAULT FALSE,
    raid_join_limit INTEGER NOT NULL DEFAULT 8
                    CHECK (raid_join_limit BETWEEN 3 AND 30),
    raid_window     INTEGER NOT NULL DEFAULT 10
                    CHECK (raid_window BETWEEN 5 AND 120),
    raid_lockdown_seconds INTEGER NOT NULL DEFAULT 300
                    CHECK (raid_lockdown_seconds BETWEEN 60 AND 3600),
    protect_join_requests BOOLEAN NOT NULL DEFAULT FALSE,
    auto_accept_join_requests BOOLEAN NOT NULL DEFAULT FALSE,
    request_limit INTEGER NOT NULL DEFAULT 8 CHECK (request_limit BETWEEN 3 AND 30),
    request_window INTEGER NOT NULL DEFAULT 10 CHECK (request_window BETWEEN 5 AND 120),
    request_lockdown_seconds INTEGER NOT NULL DEFAULT 300
                    CHECK (request_lockdown_seconds BETWEEN 60 AND 3600)
);

-- Миграция уже существующих баз: CREATE TABLE IF NOT EXISTS
-- не добавляет колонки в таблицу, которая была создана раньше.
-- Новые группы не получают включённую защиту от ботов без согласия админа.
-- Существующие значения не меняем.
ALTER TABLE chat_settings
    ALTER COLUMN block_bots SET DEFAULT FALSE;

-- Backfill settings rows for channels that were connected before this feature.
INSERT INTO chat_settings (chat_id)
SELECT chat_id FROM chats WHERE chat_type = 'channel'
ON CONFLICT (chat_id) DO NOTHING;

ALTER TABLE chat_settings
    ADD COLUMN IF NOT EXISTS block_mentions BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS block_bot_mentions BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS antiflood BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS stopwords BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS stopword_list TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS flood_limit INTEGER NOT NULL DEFAULT 5
        CHECK (flood_limit BETWEEN 2 AND 5),
    ADD COLUMN IF NOT EXISTS flood_window INTEGER NOT NULL DEFAULT 5
        CHECK (flood_window BETWEEN 3 AND 5),
    ADD COLUMN IF NOT EXISTS anti_raid BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS raid_join_limit INTEGER NOT NULL DEFAULT 8
        CHECK (raid_join_limit BETWEEN 3 AND 30),
    ADD COLUMN IF NOT EXISTS raid_window INTEGER NOT NULL DEFAULT 10
        CHECK (raid_window BETWEEN 5 AND 120),
    ADD COLUMN IF NOT EXISTS raid_lockdown_seconds INTEGER NOT NULL DEFAULT 300
        CHECK (raid_lockdown_seconds BETWEEN 60 AND 3600),
    ADD COLUMN IF NOT EXISTS protect_join_requests BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS auto_accept_join_requests BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS request_limit INTEGER NOT NULL DEFAULT 8
        CHECK (request_limit BETWEEN 3 AND 30),
    ADD COLUMN IF NOT EXISTS request_window INTEGER NOT NULL DEFAULT 10
        CHECK (request_window BETWEEN 5 AND 120),
    ADD COLUMN IF NOT EXISTS request_lockdown_seconds INTEGER NOT NULL DEFAULT 300
        CHECK (request_lockdown_seconds BETWEEN 60 AND 3600);

-- Normalize old settings that were accepted but silently clamped at runtime.
UPDATE chat_settings
SET flood_limit = LEAST(flood_limit, 5),
    flood_window = LEAST(flood_window, 5)
WHERE flood_limit > 5 OR flood_window > 5;
ALTER TABLE chat_settings
    ALTER COLUMN flood_limit SET DEFAULT 5,
    ALTER COLUMN flood_window SET DEFAULT 5;


-- CREATE TABLE / ADD COLUMN IF NOT EXISTS не гарантируют, что CHECK
-- появится на уже существующей колонке. Добавляем ограничения отдельно,
-- если их ещё нет. Если в старых данных есть недопустимые значения,
-- ALTER TABLE завершится ошибкой вместо запуска с небезопасной схемой.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_flood_limit_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_flood_limit_check
            CHECK (flood_limit BETWEEN 2 AND 5);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_flood_window_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_flood_window_check
            CHECK (flood_window BETWEEN 3 AND 5);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_flood_limit_hard_cap_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_flood_limit_hard_cap_check
            CHECK (flood_limit BETWEEN 2 AND 5);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_flood_window_hard_cap_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_flood_window_hard_cap_check
            CHECK (flood_window BETWEEN 3 AND 5);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_raid_join_limit_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_raid_join_limit_check
            CHECK (raid_join_limit BETWEEN 3 AND 30);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_raid_window_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_raid_window_check
            CHECK (raid_window BETWEEN 5 AND 120);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_raid_lockdown_seconds_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_raid_lockdown_seconds_check
            CHECK (raid_lockdown_seconds BETWEEN 60 AND 3600);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_request_limit_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_request_limit_check
            CHECK (request_limit BETWEEN 3 AND 30);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_request_window_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_request_window_check
            CHECK (request_window BETWEEN 5 AND 120);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_request_lockdown_seconds_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_request_lockdown_seconds_check
            CHECK (request_lockdown_seconds BETWEEN 60 AND 3600);
    END IF;
END
$$;

-- Уникальные вступления для обнаружения рейдов. Один пользователь
-- учитывается максимум один раз в пределах окна, даже при повторной доставке update.
-- Скользящее окно антифлуда хранится в PostgreSQL и общее для всех реплик.
-- event_key делает повторную доставку update идемпотентной, а альбом считается
-- одним событием независимо от числа содержащихся в нём сообщений.
CREATE TABLE IF NOT EXISTS flood_events (
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    event_key  TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (chat_id, user_id, event_key)
);

CREATE INDEX IF NOT EXISTS flood_events_created_idx
    ON flood_events (created_at);
CREATE INDEX IF NOT EXISTS flood_events_user_created_idx
    ON flood_events (chat_id, user_id, created_at);

CREATE TABLE IF NOT EXISTS raid_events (
    chat_id   BIGINT NOT NULL,
    user_id   BIGINT NOT NULL,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS raid_events_joined_idx
    ON raid_events (joined_at);
CREATE INDEX IF NOT EXISTS raid_events_chat_joined_idx
    ON raid_events (chat_id, joined_at);

-- Состояние временного режима защиты от массовых вступлений.
CREATE TABLE IF NOT EXISTS raid_state (
    chat_id       BIGINT PRIMARY KEY,
    locked_until  TIMESTAMPTZ NOT NULL,
    triggered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    join_count    INTEGER NOT NULL DEFAULT 0 CHECK (join_count >= 0)
);

CREATE INDEX IF NOT EXISTS raid_state_expiry_idx
    ON raid_state (locked_until);

-- Channel join-request anti-raid: unique users are counted in a rolling window.
CREATE TABLE IF NOT EXISTS channel_request_events (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS channel_request_events_requested_idx
    ON channel_request_events (requested_at);
CREATE INDEX IF NOT EXISTS channel_request_events_chat_requested_idx
    ON channel_request_events (chat_id, requested_at);

CREATE TABLE IF NOT EXISTS channel_request_state (
    chat_id BIGINT PRIMARY KEY,
    locked_until TIMESTAMPTZ NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0)
);
CREATE INDEX IF NOT EXISTS channel_request_state_expiry_idx
    ON channel_request_state (locked_until);


-- Only bot accounts observed in delivered chat_member updates can be cleaned.
-- Telegram Bot API does not expose a full member-list endpoint.
CREATE TABLE IF NOT EXISTS known_group_bots (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    username TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at TIMESTAMPTZ,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS known_group_bots_pending_idx
    ON known_group_bots (chat_id, last_seen_at)
    WHERE removed_at IS NULL;

-- Channel audience history starts when KimGuard is connected and Telegram delivers updates.
CREATE TABLE IF NOT EXISTS channel_member_events (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('join', 'leave')),
    changed_at TIMESTAMPTZ NOT NULL,
    invite_link TEXT,
    via_join_request BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (chat_id, user_id, event_type, changed_at)
);
CREATE INDEX IF NOT EXISTS channel_member_events_chat_date_idx
    ON channel_member_events (chat_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS channel_member_events_chat_type_date_idx
    ON channel_member_events (chat_id, event_type, changed_at DESC);

CREATE TABLE IF NOT EXISTS logs (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT,
    action     TEXT NOT NULL,
    details    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_user_id BIGINT,
    target_user_id BIGINT
);

ALTER TABLE logs
    ADD COLUMN IF NOT EXISTS actor_user_id BIGINT,
    ADD COLUMN IF NOT EXISTS target_user_id BIGINT;

CREATE INDEX IF NOT EXISTS logs_chat_idx ON logs (chat_id, created_at DESC);
-- Отдельный индекс нужен для глобальной очистки старых записей по дате.
CREATE INDEX IF NOT EXISTS logs_created_at_idx ON logs (created_at);

CREATE TABLE IF NOT EXISTS captcha_pending (
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    processing_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    challenge_token TEXT,
    PRIMARY KEY (chat_id, user_id)
);

-- Удаляем старый флаг: он не доказывает, что бан выставил именно бот.
ALTER TABLE captcha_pending DROP COLUMN IF EXISTS kick_started;

ALTER TABLE captcha_pending
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS processing_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS challenge_token TEXT;

DROP INDEX IF EXISTS captcha_expires_idx;
CREATE INDEX IF NOT EXISTS captcha_retry_idx
    ON captcha_pending (expires_at, processing_until, next_attempt_at);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.captcha_pending'::regclass
          AND conname = 'captcha_pending_attempts_check'
    ) THEN
        ALTER TABLE public.captcha_pending
            ADD CONSTRAINT captcha_pending_attempts_check
            CHECK (attempts >= 0);
    END IF;
END
$$;

-- Защита таблиц от доступа через публичный Supabase Data API.
-- Бот подключается к PostgreSQL напрямую; anon/authenticated доступ не нужен.
ALTER TABLE chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE captcha_pending ENABLE ROW LEVEL SECURITY;
ALTER TABLE raid_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE raid_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_request_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_request_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_member_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE known_group_bots ENABLE ROW LEVEL SECURITY;
ALTER TABLE flood_events ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE chats, chat_settings, logs, captcha_pending,
    raid_events, raid_state, channel_request_events, channel_request_state,
    channel_member_events, known_group_bots, flood_events
    FROM anon, authenticated;

REVOKE ALL ON SEQUENCE logs_id_seq FROM anon, authenticated;
