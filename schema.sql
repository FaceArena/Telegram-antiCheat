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
    block_bots      BOOLEAN NOT NULL DEFAULT TRUE,
    block_links     BOOLEAN NOT NULL DEFAULT FALSE,
    captcha         BOOLEAN NOT NULL DEFAULT FALSE,
    punishment      TEXT NOT NULL DEFAULT 'delete'
                    CHECK (punishment IN ('delete', 'mute', 'ban')),
    link_whitelist  TEXT[] NOT NULL DEFAULT '{}',
    log_chat_id     BIGINT,
    antiflood       BOOLEAN NOT NULL DEFAULT FALSE,
    stopwords       BOOLEAN NOT NULL DEFAULT FALSE,
    stopword_list   TEXT[] NOT NULL DEFAULT '{}',
    flood_limit     INTEGER NOT NULL DEFAULT 5
                    CHECK (flood_limit BETWEEN 2 AND 30),
    flood_window    INTEGER NOT NULL DEFAULT 10
                    CHECK (flood_window BETWEEN 3 AND 60)
);

-- Миграция уже существующих баз: CREATE TABLE IF NOT EXISTS
-- не добавляет колонки в таблицу, которая была создана раньше.
ALTER TABLE chat_settings
    ADD COLUMN IF NOT EXISTS antiflood BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS stopwords BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS stopword_list TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS flood_limit INTEGER NOT NULL DEFAULT 5
        CHECK (flood_limit BETWEEN 2 AND 30),
    ADD COLUMN IF NOT EXISTS flood_window INTEGER NOT NULL DEFAULT 10
        CHECK (flood_window BETWEEN 3 AND 60);


-- CREATE TABLE / ADD COLUMN IF NOT EXISTS не гарантируют, что CHECK
-- появится на уже существующей колонке. Добавляем ограничения отдельно,
-- если их ещё нет. Если в старых данных есть недопустимые значения,
-- ALTER TABLE завершится ошибкой вместо запуска с небезопасной схемой.
DO $
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_flood_limit_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_flood_limit_check
            CHECK (flood_limit BETWEEN 2 AND 30);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.chat_settings'::regclass
          AND conname = 'chat_settings_flood_window_check'
    ) THEN
        ALTER TABLE public.chat_settings
            ADD CONSTRAINT chat_settings_flood_window_check
            CHECK (flood_window BETWEEN 3 AND 60);
    END IF;
END
$;

CREATE TABLE IF NOT EXISTS logs (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT,
    action     TEXT NOT NULL,
    details    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS logs_chat_idx ON logs (chat_id, created_at DESC);

CREATE TABLE IF NOT EXISTS captcha_pending (
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS captcha_expires_idx ON captcha_pending (expires_at);

-- Защита таблиц от доступа через публичный Supabase Data API.
-- Бот подключается к PostgreSQL напрямую; anon/authenticated доступ не нужен.
ALTER TABLE chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE captcha_pending ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE chats, chat_settings, logs, captcha_pending
    FROM anon, authenticated;

REVOKE ALL ON SEQUENCE logs_id_seq FROM anon, authenticated;
