-- Run as postgres against binai. Idempotent.
--   docker exec -i -u postgres binai-postgres-1 psql -d binai -v ON_ERROR_STOP=1 < migrations/001_bot.sql
-- Then set the role's password (it is not kept in git):
--   docker exec -it -u postgres binai-postgres-1 psql -d binai -c "\password bot_rw"
BEGIN;

CREATE SCHEMA IF NOT EXISTS bot;

-- A Telegram chat linked to a site user. The company comes from core.users
-- at send time, so a user moved to another company gets the right digest.
CREATE TABLE IF NOT EXISTS bot.chats (
    chat_id        bigint PRIMARY KEY,
    user_id        bigint NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,
    muted          boolean NOT NULL DEFAULT false,
    linked_at      timestamptz NOT NULL DEFAULT now(),
    last_digest_at timestamptz
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bot_rw') THEN
        CREATE ROLE bot_rw LOGIN;
    END IF;
END $$;

GRANT CONNECT ON DATABASE binai TO bot_rw;
GRANT USAGE ON SCHEMA core, ml, bot TO bot_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON bot.chats TO bot_rw;
GRANT SELECT (id, company_id, is_active) ON core.users TO bot_rw;
GRANT SELECT (id, name) ON core.companies TO bot_rw;
GRANT SELECT ON core.lots, core.kanban_cards, core.kanban_outcomes TO bot_rw;
GRANT SELECT ON ml.recommendations, ml.recommendation_log, ml.feedback TO bot_rw;

COMMIT;
