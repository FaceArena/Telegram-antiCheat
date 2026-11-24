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
    log_chat_id     BIGINT
);

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
