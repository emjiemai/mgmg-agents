-- ============================================================================
-- MGMG Digital Command Center — PostgreSQL schema
-- Conventions:
--   * All money stored as BIGINT in tiyin (1 UZS = 100 tiyin). Never FLOAT.
--   * All timestamps are TIMESTAMPTZ, written in UTC, displayed Asia/Tashkent.
--   * Every agent side effect must land in agent_actions (security rule #2).
-- Apply:  psql -U <user> -d mgmg -f database/schema.sql
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- agent_actions — append-only audit log of every action any agent takes.
-- Security rule #2. Nothing in this table is ever updated or deleted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_actions (
    id              BIGSERIAL    PRIMARY KEY,
    occurred_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    agent           TEXT         NOT NULL,          -- 'ceo-daily-brief', 'receivables', ...
    run_id          UUID,                           -- groups all actions of one agent run
    action          TEXT         NOT NULL,          -- 'api_call', 'telegram_send', 'planner_task_create'
    target_system   TEXT         NOT NULL,          -- 'sap', 'amocrm', 'verifix', 'msgraph', 'telegram', 'postgres'
    target_ref      TEXT,                           -- endpoint, entity id, chat id
    mode            TEXT         NOT NULL DEFAULT 'read'
                        CHECK (mode IN ('read', 'write', 'notify')),
    status          TEXT         NOT NULL
                        CHECK (status IN ('success', 'failure', 'skipped', 'dry_run')),
    duration_ms     INTEGER,
    http_status     INTEGER,
    error_message   TEXT,
    payload         JSONB        NOT NULL DEFAULT '{}'::jsonb  -- request/response summary, never secrets
);

CREATE INDEX IF NOT EXISTS idx_agent_actions_occurred  ON agent_actions (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_actions_agent_run ON agent_actions (agent, run_id);
CREATE INDEX IF NOT EXISTS idx_agent_actions_failures  ON agent_actions (occurred_at DESC) WHERE status = 'failure';

-- ---------------------------------------------------------------------------
-- daily_briefs — one row per CEO brief produced (sent or not).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_briefs (
    id                  BIGSERIAL    PRIMARY KEY,
    run_id              UUID         NOT NULL,
    brief_date          DATE         NOT NULL,      -- Tashkent local date the brief covers
    generated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    sent_at             TIMESTAMPTZ,
    telegram_message_id BIGINT,
    chat_id             TEXT,
    status              TEXT         NOT NULL DEFAULT 'generated'
                            CHECK (status IN ('generated', 'sent', 'failed', 'dry_run')),
    -- Headline figures, denormalized so the brief is reproducible without re-querying SAP
    cash_total_tiyin        BIGINT,
    ar_overdue_total_tiyin  BIGINT,
    pipeline_total_tiyin    BIGINT,
    new_leads_24h           INTEGER,
    deals_without_task      INTEGER,
    employees_late          INTEGER,
    employees_absent        INTEGER,
    planner_tasks_overdue   INTEGER,
    -- Full structured payload + the exact rendered message
    sections            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    message_text        TEXT,
    source_errors       JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- systems that failed this run
    CONSTRAINT uq_daily_briefs_date UNIQUE (brief_date, run_id)
);

CREATE INDEX IF NOT EXISTS idx_daily_briefs_date ON daily_briefs (brief_date DESC);

-- ---------------------------------------------------------------------------
-- ar_aging_snapshots — daily AR aging pulled from SAP, one row per open invoice.
-- Snapshotted (not upserted) so history is preserved for trend analysis in Power BI.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ar_aging_snapshots (
    id                  BIGSERIAL    PRIMARY KEY,
    snapshot_date       DATE         NOT NULL,
    captured_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    division            TEXT,                        -- 'armin' | 'imus' | 'ondry' | 'service' | 'properties'
    doc_entry           INTEGER      NOT NULL,       -- SAP OINV.DocEntry
    doc_num             INTEGER,
    card_code           TEXT         NOT NULL,       -- SAP BP code
    card_name           TEXT,
    doc_date            DATE,
    due_date            DATE,
    days_overdue        INTEGER      NOT NULL,
    aging_bucket        TEXT         NOT NULL
                            CHECK (aging_bucket IN ('current', '1_30', '31_60', '61_90', '90_plus')),
    currency            TEXT         NOT NULL DEFAULT 'UZS',
    doc_total_tiyin     BIGINT       NOT NULL,
    paid_to_date_tiyin  BIGINT       NOT NULL DEFAULT 0,
    balance_due_tiyin   BIGINT       NOT NULL,
    sales_person_code   INTEGER,
    sales_person_name   TEXT,
    CONSTRAINT uq_ar_snapshot UNIQUE (snapshot_date, doc_entry)
);

CREATE INDEX IF NOT EXISTS idx_ar_snapshot_date   ON ar_aging_snapshots (snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_ar_snapshot_bucket ON ar_aging_snapshots (snapshot_date, aging_bucket);
CREATE INDEX IF NOT EXISTS idx_ar_snapshot_bp     ON ar_aging_snapshots (card_code, snapshot_date DESC);

-- ---------------------------------------------------------------------------
-- cash_balance_snapshots — daily cash position per bank account (SAP OACT).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cash_balance_snapshots (
    id              BIGSERIAL    PRIMARY KEY,
    snapshot_date   DATE         NOT NULL,
    captured_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    account_code    TEXT         NOT NULL,
    account_name    TEXT,
    bank_name       TEXT,                            -- 'Kapital Bank', 'Asia Alliance Bank'
    currency        TEXT         NOT NULL DEFAULT 'UZS',
    balance_tiyin   BIGINT       NOT NULL,
    CONSTRAINT uq_cash_snapshot UNIQUE (snapshot_date, account_code)
);

CREATE INDEX IF NOT EXISTS idx_cash_snapshot_date ON cash_balance_snapshots (snapshot_date DESC);

-- ---------------------------------------------------------------------------
-- amocrm_pipeline_snapshots — daily pipeline state, one row per pipeline/stage.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS amocrm_pipeline_snapshots (
    id                  BIGSERIAL    PRIMARY KEY,
    snapshot_date       DATE         NOT NULL,
    captured_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    pipeline_id         BIGINT       NOT NULL,
    pipeline_name       TEXT,
    status_id           BIGINT       NOT NULL,
    status_name         TEXT,
    division            TEXT,
    deals_count         INTEGER      NOT NULL DEFAULT 0,
    deals_value_tiyin   BIGINT       NOT NULL DEFAULT 0,
    deals_without_task  INTEGER      NOT NULL DEFAULT 0,
    new_leads_24h       INTEGER      NOT NULL DEFAULT 0,
    CONSTRAINT uq_pipeline_snapshot UNIQUE (snapshot_date, pipeline_id, status_id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_snapshot_date ON amocrm_pipeline_snapshots (snapshot_date DESC);

-- ---------------------------------------------------------------------------
-- amocrm_deal_events — raw webhook events, kept for replay and debugging.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS amocrm_deal_events (
    id                  BIGSERIAL    PRIMARY KEY,
    received_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    event_type          TEXT         NOT NULL,       -- 'add' | 'update' | 'status' | 'delete'
    lead_id             BIGINT,
    pipeline_id         BIGINT,
    status_id           BIGINT,
    responsible_user_id BIGINT,
    price_tiyin         BIGINT,
    raw_payload         JSONB        NOT NULL,
    processed_at        TIMESTAMPTZ,
    processing_status   TEXT         NOT NULL DEFAULT 'pending'
                            CHECK (processing_status IN ('pending', 'processed', 'ignored', 'failed')),
    processing_error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_deal_events_pending ON amocrm_deal_events (received_at) WHERE processing_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_deal_events_lead    ON amocrm_deal_events (lead_id, received_at DESC);

-- ---------------------------------------------------------------------------
-- approvals — human-in-the-loop gate (security rule #4).
-- An agent may only act on a row whose status is 'approved'.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approvals (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    requested_by_agent  TEXT         NOT NULL,
    category            TEXT         NOT NULL
                            CHECK (category IN ('payment', 'contract', 'hr', 'communication', 'other')),
    title               TEXT         NOT NULL,
    description         TEXT,
    amount_tiyin        BIGINT,
    currency            TEXT         DEFAULT 'UZS',
    proposed_action     JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- what will run on approval
    status              TEXT         NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'approved', 'declined', 'expired', 'executed', 'execution_failed')),
    chat_id             TEXT,
    telegram_message_id BIGINT,
    decided_at          TIMESTAMPTZ,
    decided_by          TEXT,                        -- Telegram username / user id
    expires_at          TIMESTAMPTZ  NOT NULL DEFAULT (now() + INTERVAL '24 hours'),
    executed_at         TIMESTAMPTZ,
    execution_result    JSONB
);

CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals (created_at DESC) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- alerts — every alert emitted by any agent, with its lifecycle.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id                  BIGSERIAL    PRIMARY KEY,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    agent               TEXT         NOT NULL,
    run_id              UUID,
    alert_key           TEXT         NOT NULL,       -- stable key for dedupe, e.g. 'ar_overdue:C00123'
    severity            TEXT         NOT NULL
                            CHECK (severity IN ('critical', 'warning', 'info')),
    division            TEXT,
    title               TEXT         NOT NULL,
    body                TEXT,
    amount_tiyin        BIGINT,
    owner               TEXT,                        -- responsible person
    channel             TEXT         NOT NULL DEFAULT 'telegram'
                            CHECK (channel IN ('telegram', 'teams', 'email', 'none')),
    status              TEXT         NOT NULL DEFAULT 'sent'
                            CHECK (status IN ('sent', 'acknowledged', 'resolved', 'suppressed', 'failed')),
    telegram_message_id BIGINT,
    acknowledged_at     TIMESTAMPTZ,
    acknowledged_by     TEXT,
    resolved_at         TIMESTAMPTZ,
    context             JSONB        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts (created_at DESC) WHERE status IN ('sent', 'acknowledged');
CREATE INDEX IF NOT EXISTS idx_alerts_key  ON alerts (alert_key, created_at DESC);

-- ---------------------------------------------------------------------------
-- attendance_snapshots — daily HR exceptions from Verifix.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attendance_snapshots (
    id              BIGSERIAL    PRIMARY KEY,
    snapshot_date   DATE         NOT NULL,
    captured_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    employee_id     TEXT         NOT NULL,
    employee_name   TEXT,
    division        TEXT,
    department      TEXT,
    scheduled_start TIMESTAMPTZ,
    checked_in_at   TIMESTAMPTZ,
    late_minutes    INTEGER      NOT NULL DEFAULT 0,
    status          TEXT         NOT NULL
                        CHECK (status IN ('present', 'late', 'absent', 'leave', 'holiday', 'unknown')),
    source          TEXT         NOT NULL DEFAULT 'verifix',
    CONSTRAINT uq_attendance_snapshot UNIQUE (snapshot_date, employee_id)
);

CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance_snapshots (snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_attendance_exceptions ON attendance_snapshots (snapshot_date) WHERE status IN ('late', 'absent');

-- ---------------------------------------------------------------------------
-- planner_task_snapshots — Microsoft Planner tasks, refreshed daily via Graph.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS planner_task_snapshots (
    id               BIGSERIAL    PRIMARY KEY,
    snapshot_date    DATE         NOT NULL,
    captured_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    task_id          TEXT         NOT NULL,
    plan_id          TEXT,
    bucket_id        TEXT,
    title            TEXT,
    assigned_to      TEXT,                           -- display names, comma separated
    due_at           TIMESTAMPTZ,
    percent_complete INTEGER,
    is_overdue       BOOLEAN      NOT NULL DEFAULT false,
    days_overdue     INTEGER,
    division         TEXT,
    CONSTRAINT uq_planner_snapshot UNIQUE (snapshot_date, task_id)
);

CREATE INDEX IF NOT EXISTS idx_planner_overdue ON planner_task_snapshots (snapshot_date) WHERE is_overdue;

-- ---------------------------------------------------------------------------
-- sales_summary_snapshots — daily sales totals from SAP, per division.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_summary_snapshots (
    id                  BIGSERIAL    PRIMARY KEY,
    snapshot_date       DATE         NOT NULL,
    captured_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    period_start        DATE         NOT NULL,
    period_end          DATE         NOT NULL,
    division            TEXT,
    invoices_count      INTEGER      NOT NULL DEFAULT 0,
    gross_total_tiyin   BIGINT       NOT NULL DEFAULT 0,
    currency            TEXT         NOT NULL DEFAULT 'UZS',
    CONSTRAINT uq_sales_snapshot UNIQUE (snapshot_date, period_start, period_end, division)
);

-- ---------------------------------------------------------------------------
-- Convenience views for Power BI (money exposed in UZS, not tiyin)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_ar_aging_latest AS
SELECT
    a.*,
    (a.balance_due_tiyin / 100.0)::numeric(18,2) AS balance_due_uzs
FROM ar_aging_snapshots a
WHERE a.snapshot_date = (SELECT max(snapshot_date) FROM ar_aging_snapshots);

CREATE OR REPLACE VIEW v_cash_latest AS
SELECT
    c.*,
    (c.balance_tiyin / 100.0)::numeric(18,2) AS balance_uzs
FROM cash_balance_snapshots c
WHERE c.snapshot_date = (SELECT max(snapshot_date) FROM cash_balance_snapshots);

CREATE OR REPLACE VIEW v_pipeline_latest AS
SELECT
    p.*,
    (p.deals_value_tiyin / 100.0)::numeric(18,2) AS deals_value_uzs
FROM amocrm_pipeline_snapshots p
WHERE p.snapshot_date = (SELECT max(snapshot_date) FROM amocrm_pipeline_snapshots);

-- ---------------------------------------------------------------------------
-- employees — registered via Admin Bot + OPS Manager Bot's role picker.
-- Role list is a hardcoded CHECK, mirrored in integrations/org_bot/roles.py —
-- Postgres can't import that file, keep the two in sync by hand.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employees (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id  BIGINT       NOT NULL UNIQUE,
    telegram_username TEXT,
    display_name      TEXT         NOT NULL,
    role              TEXT         NOT NULL
                          CHECK (role IN ('b2b_sotuv','it','buxgalteriya','hr','ombor',
                                           'operatsion_direktor','mobilograf','aloqa_markazi',
                                           'garmin_sotuv')),
    status            TEXT         NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    approved_by       TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_employees_role_active ON employees (role) WHERE status = 'active';

-- Idempotent fixups for an `employees` table already created by an earlier
-- version of this schema (see the same note on `tasks` below) -- both the new
-- columns AND the role CHECK, whenever a new role is added to roles.py.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS revoked_by TEXT;
ALTER TABLE employees DROP CONSTRAINT IF EXISTS employees_role_check;
ALTER TABLE employees ADD CONSTRAINT employees_role_check
    CHECK (role IN ('b2b_sotuv','it','buxgalteriya','hr','ombor',
                     'operatsion_direktor','mobilograf','aloqa_markazi',
                     'garmin_sotuv'));

-- ---------------------------------------------------------------------------
-- access_requests — pending join requests, decided by the admin via Admin Bot.
-- Partial unique index (not a plain UNIQUE) so a rejected user can re-request
-- later — only one row may be 'pending' per user at a time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS access_requests (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id  BIGINT       NOT NULL,
    telegram_username TEXT,
    display_name      TEXT,
    status            TEXT         NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    requested_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ  NOT NULL DEFAULT (now() + INTERVAL '7 days'),
    decided_at        TIMESTAMPTZ,
    decided_by        TEXT,
    admin_message_id  BIGINT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_access_requests_pending ON access_requests (telegram_user_id) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- tasks — one row per (employee recipient) of a task OPS Manager Bot routed.
-- The unique constraint is a backstop against Telegram's own occasionally-
-- duplicate webhook delivery re-dispatching the same Director message.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id                         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    director_telegram_user_id  BIGINT       NOT NULL,
    source_message_id          BIGINT       NOT NULL,
    raw_message                TEXT         NOT NULL,
    target_type                TEXT         NOT NULL CHECK (target_type IN ('employee','agent')),
    target_role                TEXT,
    target_agent                TEXT,
    assigned_employee_id       UUID         REFERENCES employees(id),
    task_summary                TEXT        NOT NULL,
    has_media                   BOOLEAN     NOT NULL DEFAULT false,  -- delivered via copyMessage, not sendMessage
    status                      TEXT        NOT NULL DEFAULT 'sent' CHECK (status IN ('sent','started','done')),
    telegram_message_id        BIGINT,
    started_at                  TIMESTAMPTZ,
    started_by                  TEXT,
    completed_at                TIMESTAMPTZ,
    completed_by                TEXT,
    CONSTRAINT uq_task_dispatch UNIQUE (director_telegram_user_id, source_message_id, assigned_employee_id)
);

-- Idempotent fixups for a `tasks` table already created by an earlier version
-- of this schema (no migration tooling in this project -- CREATE TABLE IF NOT
-- EXISTS is a no-op against an existing table, so new columns/constraints on
-- an already-live table need an explicit ALTER here every time).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS has_media BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS started_by TEXT;
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN ('sent', 'started', 'done'));

DROP INDEX IF EXISTS idx_tasks_open;
CREATE INDEX idx_tasks_open ON tasks (created_at DESC) WHERE status IN ('sent', 'started');

-- ---------------------------------------------------------------------------
-- pending_dispatches — a Director's media/file with no (or an ambiguous)
-- caption, waiting on a role-picker tap to know who it's actually for.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_dispatches (
    id                         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    director_telegram_user_id  BIGINT       NOT NULL,
    source_message_id          BIGINT       NOT NULL,
    caption                    TEXT,
    resolved_at                TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pending_dispatches_open ON pending_dispatches (created_at DESC) WHERE resolved_at IS NULL;

-- ---------------------------------------------------------------------------
-- task_updates — a two-way employee<->Director message thread, relayed live
-- through OPS Manager Bot and kept for a paper trail ("something always
-- might happen"). ``task_id`` is set when the message is about a specific
-- task (progress notes at any stage) and NULL for a general message not
-- tied to any task -- being able to talk to the Director through this bot
-- doesn't require an open task to exist. ``direction`` distinguishes an
-- employee's message from the Director's reply to it; ``director_message_id``
-- (only set on the employee_to_director side) is the relay's message id in
-- the Director's chat, so a reply-to-that-message routes back to the right
-- employee without going through task classification.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_updates (
    id                          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id                     UUID         REFERENCES tasks(id),
    employee_telegram_user_id   BIGINT       NOT NULL,
    message_text                TEXT         NOT NULL,
    director_telegram_user_id   BIGINT,
    director_message_id         BIGINT,
    direction                   TEXT         NOT NULL DEFAULT 'employee_to_director'
                                     CHECK (direction IN ('employee_to_director', 'director_to_employee')),
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Schema evolution: task_id was NOT NULL originally (task-scoped updates
-- only) -- relaxed so a general, not-tied-to-a-task message can be logged
-- in the same thread. The other three columns are new entirely.
ALTER TABLE task_updates ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE task_updates ADD COLUMN IF NOT EXISTS director_telegram_user_id BIGINT;
ALTER TABLE task_updates ADD COLUMN IF NOT EXISTS director_message_id BIGINT;
ALTER TABLE task_updates ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'employee_to_director'
    CHECK (direction IN ('employee_to_director', 'director_to_employee'));

CREATE INDEX IF NOT EXISTS idx_task_updates_task ON task_updates (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_updates_relay ON task_updates (director_telegram_user_id, director_message_id)
    WHERE direction = 'employee_to_director';

-- ---------------------------------------------------------------------------
-- conversation_turns — OPS Manager Bot's short-term memory per Director, so
-- follow-up questions ("what about the 25th one") resolve correctly instead
-- of every message being treated as the first one ever sent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_turns (
    id                 BIGSERIAL    PRIMARY KEY,
    telegram_user_id   BIGINT       NOT NULL,
    role               TEXT         NOT NULL CHECK (role IN ('director', 'bot')),
    content            TEXT         NOT NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversation_turns_recent ON conversation_turns (telegram_user_id, created_at DESC);
