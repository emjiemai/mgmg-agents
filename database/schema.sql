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

-- The invoice as written (2026-10-06). SAP's DocTotal/PaidToDate are its local
-- currency, USD, even for invoices written in so'm (every one so far), so the
-- Director couldn't find "$9,764.31" in SAP. DocCur, DocTotalFC and PaidFC are
-- kept when the gateway sends them; NULL until then.
ALTER TABLE ar_aging_snapshots ADD COLUMN IF NOT EXISTS doc_currency TEXT;
ALTER TABLE ar_aging_snapshots ADD COLUMN IF NOT EXISTS doc_total_fc_tiyin BIGINT;
ALTER TABLE ar_aging_snapshots ADD COLUMN IF NOT EXISTS paid_fc_tiyin BIGINT;

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
-- Columns listed, not a.*: CREATE OR REPLACE VIEW must keep the existing
-- columns in their order and can only add new ones at the end.
CREATE OR REPLACE VIEW v_ar_aging_latest AS
SELECT
    a.id, a.snapshot_date, a.captured_at, a.division, a.doc_entry, a.doc_num, a.card_code, a.card_name,
    a.doc_date, a.due_date, a.days_overdue, a.aging_bucket, a.currency, a.doc_total_tiyin,
    a.paid_to_date_tiyin, a.balance_due_tiyin, a.sales_person_code, a.sales_person_name,
    (a.balance_due_tiyin / 100.0)::numeric(18,2) AS balance_due_uzs,
    a.doc_currency, a.doc_total_fc_tiyin, a.paid_fc_tiyin
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
-- crm_stats_snapshots — whole-CRM aggregate (contacts, conversion), one row
-- per day. Split from amocrm_pipeline_snapshots (one row per stage) because
-- total_contacts/conversion_rate are whole-CRM numbers, not per-stage ones —
-- repeating them on every stage row would be redundant and easy to get out
-- of sync if only some rows were updated.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crm_stats_snapshots (
    snapshot_date     DATE         PRIMARY KEY,
    captured_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    total_deals       INTEGER      NOT NULL DEFAULT 0,
    total_value_tiyin BIGINT       NOT NULL DEFAULT 0,
    total_contacts    INTEGER      NOT NULL DEFAULT 0,
    won_deals         INTEGER      NOT NULL DEFAULT 0,
    won_value_tiyin   BIGINT       NOT NULL DEFAULT 0,
    conversion_rate   NUMERIC(6,2) NOT NULL DEFAULT 0
);

CREATE OR REPLACE VIEW v_crm_stats_latest AS
SELECT * FROM crm_stats_snapshots
WHERE snapshot_date = (SELECT max(snapshot_date) FROM crm_stats_snapshots);

-- ---------------------------------------------------------------------------
-- crm_employee_reports — synced from the in-house CRM's /reports endpoint
-- (employee daily-standup-style submissions). Keyed by the CRM's own report
-- id, not a daily snapshot -- each report already has its own stable id and
-- submitted_at, so this is a plain upsert-by-id sync, not a per-day table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crm_employee_reports (
    id            BIGINT       PRIMARY KEY,
    manager_id    BIGINT,
    manager_name  TEXT,
    report_type   TEXT,
    report_date   DATE,
    content       TEXT,
    submitted_at  TIMESTAMPTZ,
    synced_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crm_employee_reports_date ON crm_employee_reports (report_date DESC);

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
                          CHECK (role IN ('b2b_sotuv','it','buxgalteriya','moliya','hr','ombor',
                                           'operatsion_direktor','mobilograf','aloqa_markazi',
                                           'garmin_sotuv','londry','garmin_manager','texnik')),
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
    CHECK (role IN ('b2b_sotuv','it','buxgalteriya','moliya','hr','ombor',
                     'operatsion_direktor','mobilograf','aloqa_markazi',
                     'garmin_sotuv','londry','garmin_manager','texnik'));

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

-- Second approval step: after the admin accepts the PERSON, the role they pick
-- also needs the admin's Accept before they're registered. Without it, anyone
-- past the first step could pick Operatsion Direktor and receive every report
-- and give the bot orders. role_status NULL = no role picked yet.
ALTER TABLE access_requests ADD COLUMN IF NOT EXISTS requested_role TEXT;
ALTER TABLE access_requests ADD COLUMN IF NOT EXISTS role_status TEXT;
ALTER TABLE access_requests ADD COLUMN IF NOT EXISTS role_admin_message_id BIGINT;
ALTER TABLE access_requests ADD COLUMN IF NOT EXISTS role_decided_at TIMESTAMPTZ;
ALTER TABLE access_requests ADD COLUMN IF NOT EXISTS role_decided_by TEXT;
ALTER TABLE access_requests DROP CONSTRAINT IF EXISTS access_requests_role_status_check;
ALTER TABLE access_requests ADD CONSTRAINT access_requests_role_status_check
    CHECK (role_status IS NULL OR role_status IN ('pending', 'approved', 'rejected'));

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

-- A3 task tracker: the deadline the Director stated (or picked with one tap),
-- and when the one reminder / one overdue notice went out, so a cron retry
-- never nags twice.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due_date DATE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS overdue_notified_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks (due_date) WHERE due_date IS NOT NULL;

-- The one task register (the owner's spec of 11.10.2026, Day 1;
-- integrations/org_bot/task_register.py). Every task has a code
-- (T-2026-10-0001), states sent (created) → started (accepted) → blocked /
-- review (done, proof sent, waiting for the one who assigned it) → done
-- (closed) or back to started (returned), or cancelled; "done" needs proof.
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check
    CHECK (status IN ('sent', 'started', 'blocked', 'review', 'done', 'cancelled'));
CREATE SEQUENCE IF NOT EXISTS task_code_seq;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS code TEXT;
UPDATE tasks SET code = 'T-' || to_char(created_at AT TIME ZONE 'Asia/Tashkent', 'YYYY-MM') || '-'
                        || lpad(nextval('task_code_seq')::text, 4, '0')
WHERE code IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_code ON tasks (code);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS awaiting TEXT CHECK (awaiting IN ('evidence', 'blocker'));
-- 'reschedule' (2026-11-10): the next message is the reason for a new deadline.
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_awaiting_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_awaiting_check CHECK (awaiting IN ('evidence', 'blocker', 'reschedule'));
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS awaiting_since TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS evidence_text TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS evidence_message_id BIGINT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS blocker TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS review_message_id BIGINT;
-- 2026-11-04: after ✅ / ⛔ the bot asks in a NEW message (an edited day-old card wasn't seen: 03.11, six ✅, no
-- proof); a reply to it or to the card is that task's proof / reason however much later. ✅ without proof by the
-- next morning: the person is told once in the morning message (proof_reminded_at).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS awaiting_message_id BIGINT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS proof_reminded_at TIMESTAMPTZ;

-- Every step of every task, append-only: who, when, what (and the blocker /
-- proof text). Holds work content — not in /db.
CREATE TABLE IF NOT EXISTS task_events (
    id         BIGSERIAL    PRIMARY KEY,
    task_id    UUID         NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    event      TEXT         NOT NULL CHECK (event IN ('created', 'accepted', 'blocked', 'unblocked', 'submitted',
                                                      'closed', 'returned', 'cancelled')),
    actor      TEXT,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events (task_id, at);
-- 'repeated' (2026-10-17): the Director gave the same task again while it was still open — no new row,
-- the person is reminded on the existing card.
ALTER TABLE task_events DROP CONSTRAINT IF EXISTS task_events_event_check;
ALTER TABLE task_events ADD CONSTRAINT task_events_event_check CHECK (event IN (
    'created', 'accepted', 'blocked', 'unblocked', 'submitted', 'closed', 'returned', 'cancelled', 'repeated',
    'reschedule_asked', 'rescheduled', 'reschedule_refused',  -- a new deadline asked / given / refused (10.11)
    'resubmit_asked',  -- the proof never reached the assigner, asked again (12.11) — not rework
    'accepter_set',  -- a TEAM_OF manager checks the proof instead of the assigner (12.11); note = his name
    'depends_set', 'depends_done',  -- waits for another T- code / that one finished or was cancelled (12.11)
    'helper_asked', 'helper_done',  -- ⛔ → a help task given / accepted (12.11, TASK_HELPER_ENABLED); note = who / code
    'escalated'));  -- the ladder told a department manager / deputy / the Director (12.11); note = 'manager:<name>' / 'director'
-- Task improvements (2026-11-10, the owner's МИЯ document §5.2 and the agents catalogue): priority P1–P4 (1 =
-- шошилинч, 2 = муҳим — the default, 3 = оддий, 4 = кутиши мумкин); a new deadline the employee asked for, waiting
-- for the assigner's ✅ / ❌ (proposed_due), and how many times the deadline was moved (reschedules).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 2;
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_priority_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_priority_check CHECK (priority BETWEEN 1 AND 4);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS proposed_due DATE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reschedules INTEGER NOT NULL DEFAULT 0;
-- Rework (2026-11-12): how many times the assigner returned it (↩️ + a reason code; the reason is the 'returned'
-- event's note). A delivery failure ('resubmit_asked') is never counted.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS returns INTEGER NOT NULL DEFAULT 0;
-- Accepting manager (2026-11-12): the TEAM_OF manager the Director picked on the draft checks the proof (✅ / ↩️)
-- instead of him; NULL = the assigner, as before. Only when every recipient is in that manager's team.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS accepter_employee_id UUID REFERENCES employees(id) ON DELETE SET NULL;
-- Expected result (2026-11-12): what must exist when it is done, in the Director's words (the classifier extracts it,
-- he confirms it on the draft; NULL = he didn't say). proof_hint = the advisory AI line on the review card
-- (TASK_PROOF_HINT_ENABLED, off by default) — kept for history, never a status. Both hidden from /db (db_viewer).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS expected_result TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS proof_hint TEXT;
-- Dependencies (2026-11-12): the task waits for another one (an explicit T- code in the Director's words, kept or
-- dropped on the draft). Waiting = depends_on set, not released yet, and some copy of that task's shared group still
-- open. dependency_done_at = released (the one it waited for was accepted or cancelled) — set once.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS depends_on UUID REFERENCES tasks(id) ON DELETE SET NULL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS dependency_done_at TIMESTAMPTZ;
-- ⛔ helper (2026-11-12, TASK_HELPER_ENABLED, off by default): a help task the assigner gave for a blocked task —
-- helper_for = that blocked task. Accepting it never unblocks the parent by itself: its assignee is asked ▶️ / ⛔.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS helper_for UUID REFERENCES tasks(id) ON DELETE SET NULL;
-- Escalation ladder (2026-11-12): 0 = nobody above the assigner told yet; 1 = the department manager (TEAM_OF) or his
-- deputy got it (the day-0 overdue list counts); 2 = the Director got it. Set only after a send went out; reset
-- together with reminded_at / overdue_notified_at (new deadline, return, release, unblock, help done).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS escalation_level SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ;

DROP INDEX IF EXISTS idx_tasks_open;
CREATE INDEX idx_tasks_open ON tasks (created_at DESC) WHERE status IN ('sent', 'started', 'blocked', 'review');

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

-- ---------------------------------------------------------------------------
-- sap_gateway_snapshots — raw pushes from the SAP gateway's own machine for
-- every tool EXCEPT get_invoices (which has its own richer, bucketed table:
-- ar_aging_snapshots). One generic table for get_orders/get_products/
-- get_customers/get_warehouses/get_inventory/get_payments rather than six
-- separate rigid schemas, because the gateway's exact response shape for
-- these six is not confirmed the way get_sales/get_invoices' was (that one
-- was verified against a real documented example response; these six are
-- built from SAP Business One's standard field names, not a confirmed
-- example) -- ``raw`` keeps the full row exactly as returned no matter what
-- its real field names turn out to be, so nothing is lost even if
-- ``natural_key`` extraction guesses wrong on the first real push.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sap_gateway_snapshots (
    id            BIGSERIAL    PRIMARY KEY,
    tool          TEXT         NOT NULL
                      CHECK (tool IN ('orders', 'products', 'customers', 'warehouses', 'inventory', 'payments')),
    snapshot_date DATE         NOT NULL,
    captured_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    natural_key   TEXT         NOT NULL,  -- best-effort: DocEntry/ItemCode/CardCode/WhsCode, or a row hash if none found
    raw           JSONB        NOT NULL,  -- the full row exactly as the gateway returned it
    CONSTRAINT uq_sap_gateway_snapshot UNIQUE (tool, snapshot_date, natural_key)
);

CREATE INDEX IF NOT EXISTS idx_sap_gateway_snapshots_tool ON sap_gateway_snapshots (tool, snapshot_date DESC);

-- Each kind's latest day by a join to its max (2026-11-09): the correlated subquery ran once per row — a GROUP BY over
-- the view took 1.3 s (25 calls, up to 4.2 s), one kind 125 ms; the join takes 6 ms / 2 ms. Same columns.
CREATE OR REPLACE VIEW v_sap_gateway_latest AS
SELECT s.*
FROM sap_gateway_snapshots s
JOIN (SELECT tool, max(snapshot_date) AS latest FROM sap_gateway_snapshots GROUP BY tool) m
  ON m.tool = s.tool AND s.snapshot_date = m.latest;

-- Complete pushes (2026-10-03): gateway tools without the 100-row cap
-- (docs/sap-gateway-tools.md) send more kinds of data into the same table
-- (push_handler.FULL_DATASETS). A complete push replaces that day's rows for
-- its kind; only a few days are kept.
ALTER TABLE sap_gateway_snapshots DROP CONSTRAINT IF EXISTS sap_gateway_snapshots_tool_check;
ALTER TABLE sap_gateway_snapshots ADD CONSTRAINT sap_gateway_snapshots_tool_check CHECK (tool IN (
    'orders', 'products', 'customers', 'warehouses', 'inventory', 'payments',
    'sales', 'sales_lines', 'payments_out', 'ap_open', 'po_open', 'sales_people',
    'equipment', 'service_calls', 'service_contracts', 'stock_value', 'supplier_balances',
    'stock_items',  -- get_stock_value's item rows, kept (2026-10-16)
    'item_sales_90d'  -- quantity sold per item and warehouse, 90 days (gateway get_item_sales, 2026-10-16)
));

-- Billz → SAP check (2026-10-03, docs/agent-specs/17-billz-sap-check.md):
-- every cheque in the shops' Billz tills must reach SAP as an A/R invoice the
-- same day. One row per checked day.
CREATE TABLE IF NOT EXISTS billz_sap_checks (
    check_date  DATE         PRIMARY KEY,
    checked_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    status      TEXT         NOT NULL,  -- ok | problems | no_sap | stale_sap | no_billz
    result      JSONB        NOT NULL,
    sent_at     TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- daily_reports — one row per employee per working day. Created by
-- agents/daily-reports at 16:00 Tashkent (status 'asked') and filled in when
-- the employee answers OPS Manager Bot (status 'submitted').
--
-- Separate from crm_employee_reports on purpose: that table mirrors what
-- employees type into the CRM's own web app (synced read-only from the CRM
-- API, so it can't record who was asked or who stayed silent). This one is
-- the bot's own collection — the KPI numbers, the ask/answer timestamps, and
-- the rows that never came back, which is what any discipline measure needs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_reports (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    report_date        DATE         NOT NULL,
    employee_id        UUID         NOT NULL REFERENCES employees(id),
    telegram_user_id   BIGINT       NOT NULL,
    role               TEXT         NOT NULL,
    status             TEXT         NOT NULL DEFAULT 'asked' CHECK (status IN ('asked', 'submitted')),
    content            TEXT,
    metrics            JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- {"calls": 20, ...}, keys from org_bot/kpi.py
    tasks_done         INTEGER      NOT NULL DEFAULT 0,            -- counted from tasks, not self-reported
    prompt_message_id  BIGINT,                                     -- the 16:00 ask, so a reply to it is unambiguous
    asked_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    reminded_at        TIMESTAMPTZ,
    submitted_at       TIMESTAMPTZ,
    CONSTRAINT uq_daily_report UNIQUE (report_date, employee_id)
);

CREATE INDEX IF NOT EXISTS idx_daily_reports_open ON daily_reports (report_date) WHERE status = 'asked';
-- One short follow-up when a report says nothing concrete ("ok", "ishladim").
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS followup_asked_at TIMESTAMPTZ;
-- The 17:00 reminder's message id: a reply to it is as clearly the report as
-- a reply to the 16:00 ask.
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS reminder_message_id BIGINT;
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS followup_answered_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_daily_reports_employee ON daily_reports (employee_id, report_date DESC);

-- ---------------------------------------------------------------------------
-- permission_requests — written permission requests under EMJ-SOP-ADM-01
-- ("Ёзма рухсат ва тасдиқ олиш тартиби"). One row per issue: the SOP requires
-- one request per matter and forbids splitting a request to get under a limit.
--
-- This table plus permission_request_events IS the "ягона реестр" the SOP's
-- documents coordinator is supposed to keep (§5): number, time received,
-- who is responsible, the decision, and the execution state.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS permission_request_no_seq;

CREATE TABLE IF NOT EXISTS permission_requests (
    id                          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    request_no                  TEXT         UNIQUE,          -- EMJ-2026-0001, assigned on submit
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    submitted_at                TIMESTAMPTZ,                  -- "Қабул қилинган сана ва вақт"
    requester_employee_id       UUID         REFERENCES employees(id),
    requester_telegram_user_id  BIGINT       NOT NULL,
    requester_name              TEXT         NOT NULL,
    requester_role              TEXT         NOT NULL,        -- doubles as "Бўлим"/position
    subject                     TEXT,                         -- "Нимага рухсат сўралади"
    reason                      TEXT,                         -- "Сабаб ва таклиф"
    amount_tiyin                BIGINT,                       -- "Сумма", 0 when no cost
    amount_raw                  TEXT,                         -- what they typed, when it isn't a plain number
    currency                    TEXT         NOT NULL DEFAULT 'UZS',
    execute_by                  TEXT,                         -- "Бажариш муддати"
    decision_needed_by          TEXT,                         -- "Қарор керак бўлган сана ва вақт"
    urgency                     TEXT,                         -- "Шошилинчлик сабаби" or "оддий"
    attachments                 TEXT,                         -- "Иловалар"
    pending_field               TEXT,                         -- which answer the bot is waiting for
    status                      TEXT         NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'submitted', 'approved', 'approved_conditional',
                          'rejected', 'info_needed', 'cancelled')),
    submitted_to                TEXT,                         -- "Кимга тақдим этилади"
    decided_at                  TIMESTAMPTZ,
    decided_by                  TEXT,                         -- approver name + role
    decided_by_telegram_user_id BIGINT,
    approved_terms              TEXT,                         -- "Тасдиқланган сумма ва амал қилиш муддати"
    decision_note               TEXT,                         -- "Шартлар ёки қарор сабаби"
    pending_decision            TEXT,                         -- outcome picked, waiting for the approver's note
    note_awaited_from           BIGINT,                       -- that approver's Telegram id
    completion_note             TEXT,                         -- "Якун далили" (§5)
    completed_at                TIMESTAMPTZ
);

-- One open draft per person: the bot fills a request by asking one question at
-- a time, and a second half-finished draft would make every answer ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uq_permission_draft_per_user
    ON permission_requests (requester_telegram_user_id) WHERE status = 'draft';
CREATE INDEX IF NOT EXISTS idx_permission_requests_open
    ON permission_requests (created_at DESC) WHERE status IN ('submitted', 'info_needed');
CREATE INDEX IF NOT EXISTS idx_permission_requests_requester
    ON permission_requests (requester_telegram_user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- permission_request_events — append-only history for each request. SOP §3
-- accepts an electronic approval only where the approver AND the decision
-- history are preserved, so nothing here is ever updated or deleted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS permission_request_events (
    id                      BIGSERIAL    PRIMARY KEY,
    request_id              UUID         NOT NULL REFERENCES permission_requests(id) ON DELETE CASCADE,
    occurred_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
    actor                   TEXT         NOT NULL,
    actor_telegram_user_id  BIGINT,
    action                  TEXT         NOT NULL,  -- created | submitted | decided | info_needed | completed | cancelled
    detail                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_permission_events_request
    ON permission_request_events (request_id, occurred_at);

-- The SOP form asks for position and department explicitly ("Ходимнинг исми
-- ва лавозими", "Бўлим"); the bot asks for both rather than guessing them
-- from the role, so nothing on the filed form is invented.
ALTER TABLE permission_requests ADD COLUMN IF NOT EXISTS requester_position TEXT;
ALTER TABLE permission_requests ADD COLUMN IF NOT EXISTS requester_full_name TEXT;
-- The approver's own full name as they typed it for the SOP form (not the
-- Telegram profile name), asked once and reused on later decisions.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS full_name TEXT;
-- When the bot asked this employee for their name (integrations/org_bot/names.py):
-- once asked, their next message is taken as the answer.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS name_asked_at TIMESTAMPTZ;
-- When the employee last asked (/ism) to change their name — one request
-- an hour at most, so the admin isn't flooded.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS name_change_requested_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- employee_changes — every change to an employee's name or role: who it
-- was before, who changed it, when. A name on an old report or document can
-- always be traced (documents also keep the name they were signed with).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employee_changes (
    id           BIGSERIAL    PRIMARY KEY,
    employee_id  UUID         NOT NULL REFERENCES employees(id),
    field        TEXT         NOT NULL CHECK (field IN ('full_name', 'role')),
    old_value    TEXT,
    new_value    TEXT,
    changed_by   TEXT         NOT NULL,
    changed_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_employee_changes_employee ON employee_changes (employee_id, changed_at DESC);
ALTER TABLE employee_changes DROP CONSTRAINT IF EXISTS employee_changes_field_check;
-- Every field store.log_employee_change is called with (selfcheck compares them).
-- 'data_access' and 'lang' (2026-10-10) were missing until 15.10: each /xodimlar
-- SAP / 1C / Billz / language toggle failed after its update, Telegram re-sent
-- the press and the toggle flipped back and forth.
ALTER TABLE employee_changes ADD CONSTRAINT employee_changes_field_check
    CHECK (field IN ('full_name', 'role', 'workdays', 'address_form', 'ai_chat', 'cheer', 'reports',
                     'data_access', 'lang', 'deputy'));

-- Who also works at the weekend (set by the admin on the /xodimlar card).
-- Everyone works Monday–Friday; on Saturday/Sunday only these are asked for
-- a daily report, so nobody else is counted as "didn't report".
ALTER TABLE employees ADD COLUMN IF NOT EXISTS works_saturday BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS works_sunday BOOLEAN NOT NULL DEFAULT false;

-- How friendly messages address someone (2026-09-30): first name + 'ака'
-- or 'опа', set by the admin on the /xodimlar card — never guessed from the
-- name. NULL = the first name alone.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS address_form TEXT CHECK (address_form IN ('aka', 'opa'));

-- ---------------------------------------------------------------------------
-- pending_relays — an employee's message held until they confirm it should
-- go to the Director ("Директорга юборилсинми?"). One accidental message to
-- the CEO is one too many; nothing is relayed without the tap.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_relays (
    id                          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_telegram_user_id   BIGINT       NOT NULL,
    task_id                     UUID         REFERENCES tasks(id),
    message_text                TEXT         NOT NULL,
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at                 TIMESTAMPTZ,
    outcome                     TEXT         CHECK (outcome IN ('sent', 'cancelled', 'report'))
);
-- 'report': the employee chose "this is my daily report" for a message that
-- could have been either (see ops_manager._ask_report_or_relay).
ALTER TABLE pending_relays DROP CONSTRAINT IF EXISTS pending_relays_outcome_check;
ALTER TABLE pending_relays ADD CONSTRAINT pending_relays_outcome_check
    CHECK (outcome IN ('sent', 'cancelled', 'report', 'ai', 'expired'));  -- expired: untapped by 22:00 (2026-10-17)
ALTER TABLE permission_requests ADD COLUMN IF NOT EXISTS department TEXT;

-- ---------------------------------------------------------------------------
-- client_feedback — complaints clients leave through the QR code (opinions
-- too, before 2026-09-28) (integrations/api/feedback_page.py). Name and phone are optional;
-- both empty = anonymous. No IP address or device data is stored.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_feedback (
    id                   BIGSERIAL    PRIMARY KEY,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    place                TEXT         NOT NULL,   -- 'laundry' | 'garmin', chosen on the page
    kind                 TEXT         NOT NULL CHECK (kind IN ('feedback', 'complaint')),
    message              TEXT         NOT NULL,
    contact_name         TEXT,
    phone                TEXT,
    director_message_id  BIGINT                   -- set once it reached the Director
);
CREATE INDEX IF NOT EXISTS idx_client_feedback_created ON client_feedback (created_at DESC);
-- 2026-09-28: one QR code for the whole company; rows from that day until the
-- place picker came back (same day) have no place.
ALTER TABLE client_feedback ALTER COLUMN place DROP NOT NULL;
-- 2026-10-04: the branch the client picked (beshqozon / vuzgorodok / abay / minor);
-- NULL on complaints from before the choice existed.
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS branch TEXT;
-- 2026-10-13 (the owner's spec of 11.10, A4/A5): a complaint is a ticket that
-- goes to its department (routes.py), not to the Boss: a code from the task
-- sequence, open → resolved (with a reason code), the cards each recipient
-- got, and when the 1-hour status question went out. NULL status = a
-- complaint from before tickets.
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS code TEXT;
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS ticket_status TEXT CHECK (ticket_status IN ('open', 'resolved'));
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS routed_to TEXT;
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS cards JSONB NOT NULL DEFAULT '[]';
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS asked_at TIMESTAMPTZ;
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS not_yet_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS asks INTEGER NOT NULL DEFAULT 0;
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS resolved_by TEXT;
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS reason_code TEXT;
-- The page's language the client wrote in, so every card (also once resolved) shows it.
ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS lang TEXT;
CREATE INDEX IF NOT EXISTS idx_client_feedback_open ON client_feedback (created_at) WHERE ticket_status = 'open';

-- (cheer_messages / cheer_deliveries: team cheer was removed 2026-10-17; the owner drops the tables.)

-- ---------------------------------------------------------------------------
-- KPI (Director's criteria, 2026-09-30) — integrations/org_bot/kpi_score.py
-- kpi_goals: monthly goals with a number (OKR / goals / results / sales
--   deals). A 'draft' row is the Director mid-way through /maqsad; the
--   awaiting_* pair is someone about to type an actual result.
-- kpi_ratings: the Director's 1–5 marks (performance, communication,
--   interaction, qualifications), one row per person per month.
-- kpi_periods: which months' rating requests / final tables went out, so a
--   retried cron never sends them twice.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kpi_goals (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id          UUID         NOT NULL REFERENCES employees (id),
    month                DATE         NOT NULL,                -- first day of the month
    title                TEXT,                                 -- NULL while a draft
    target               NUMERIC,
    actual               NUMERIC,
    actual_by            TEXT,
    actual_at            TIMESTAMPTZ,
    status               TEXT         NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'cancelled')),
    set_by_telegram_id   BIGINT       NOT NULL,
    awaiting_actual_by   BIGINT,
    awaiting_since       TIMESTAMPTZ,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kpi_goals_month ON kpi_goals (month, employee_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS kpi_ratings (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id     UUID         NOT NULL REFERENCES employees (id),
    month           DATE         NOT NULL,
    performance     SMALLINT     CHECK (performance BETWEEN 1 AND 5),
    communication   SMALLINT     CHECK (communication BETWEEN 1 AND 5),
    interaction     SMALLINT     CHECK (interaction BETWEEN 1 AND 5),
    qualifications  SMALLINT     CHECK (qualifications BETWEEN 1 AND 5),
    rated_by        BIGINT,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (employee_id, month)
);

CREATE TABLE IF NOT EXISTS kpi_periods (
    month                  DATE         PRIMARY KEY,
    ratings_requested_at   TIMESTAMPTZ,
    final_sent_at          TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- leads / lead_assignments / lead_checkins — the Lead Agent's leads handed
-- to B2B sales (agents/lead-handout, integrations/org_bot/leads.py; stopped 07.10, back 28.10 — see below).
-- 08:00: each B2B sales person at work gets one lead (today's new ones
-- first, then the best unworked older ones). 15:00: "how is it going?" with
-- жараёнда / рад этилди / бажарилди for every lead still open; an open lead
-- is asked again every day until it is closed.
-- ``leads`` mirrors the Google leads sheet (imported every morning, keyed by
-- the sheet's own dedupe_key); ``brief`` is the Uzbek Cyrillic summary the
-- card shows, written once.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
    id                 BIGSERIAL    PRIMARY KEY,
    dedupe_key         TEXT         NOT NULL UNIQUE,
    company_name       TEXT,
    project_name       TEXT,
    industry           TEXT,
    location           TEXT,
    project_stage      TEXT,
    signal             TEXT,
    signal_source_url  TEXT,
    contact_name       TEXT,
    contact_role       TEXT,
    contact_method     TEXT,
    priority           TEXT,
    confidence         NUMERIC,
    track              TEXT,
    date_added         DATE,
    brief              TEXT,
    imported_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- One lead goes to one person, once; one new lead per person per day.
CREATE TABLE IF NOT EXISTS lead_assignments (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id       BIGINT       NOT NULL UNIQUE REFERENCES leads (id),
    employee_id   UUID         NOT NULL REFERENCES employees (id),
    assigned_on   DATE         NOT NULL,
    status        TEXT         NOT NULL DEFAULT 'new'
                               CHECK (status IN ('new', 'in_progress', 'dismissed', 'done')),
    outcome       TEXT,        -- the reason (dismissed) or the result (done)
    closed_on     DATE,
    message_id    BIGINT,      -- the 08:00 card
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (employee_id, assigned_on)
);
CREATE INDEX IF NOT EXISTS idx_lead_assignments_open ON lead_assignments (employee_id) WHERE status IN ('new', 'in_progress');

-- One 15:00 question per open lead per day; the answer and the one follow-up.
CREATE TABLE IF NOT EXISTS lead_checkins (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_id        UUID         NOT NULL REFERENCES lead_assignments (id) ON DELETE CASCADE,
    checkin_day          DATE         NOT NULL,
    telegram_user_id     BIGINT       NOT NULL,
    message_id           BIGINT,
    status               TEXT         CHECK (status IN ('in_progress', 'dismissed', 'done')),
    answered_at          TIMESTAMPTZ,
    question             TEXT,        -- the follow-up asked after the tap, if any
    question_message_id  BIGINT,
    question_asked_at    TIMESTAMPTZ,
    note                 TEXT,        -- the typed answer to it
    noted_at             TIMESTAMPTZ,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (assignment_id, checkin_day)
);
CREATE INDEX IF NOT EXISTS idx_lead_checkins_question ON lead_checkins (telegram_user_id, question_asked_at DESC)
    WHERE note IS NULL AND question IS NOT NULL;

-- 2026-10-28 (the Boss): the hand-out is back as the B2B lead tracker (lead_tracker.py, spec 14) — each lead has
-- a number (leads.id, «Лид №33»); at 15:00 the seller is asked: contacted? (бугун / кеча / ҳали йўқ), then sold
-- (what and how much, typed) / not sold (a reason) / still going. Days to contact, sales and the reasons make the
-- Boss's statistics (/lidlar, PULSE, the PDF table). A lead not contacted in a week is closed as «алоқага чиқилмади».
ALTER TABLE lead_assignments ADD COLUMN IF NOT EXISTS contacted_on DATE;
ALTER TABLE lead_assignments ADD COLUMN IF NOT EXISTS sold_amount NUMERIC;
ALTER TABLE lead_assignments ADD COLUMN IF NOT EXISTS sold_currency TEXT;  -- 'UZS' | 'USD'
ALTER TABLE lead_assignments ADD COLUMN IF NOT EXISTS closed_by TEXT;      -- 'seller' | 'auto'
ALTER TABLE lead_checkins DROP CONSTRAINT IF EXISTS lead_checkins_status_check;
ALTER TABLE lead_checkins ADD CONSTRAINT lead_checkins_status_check
    CHECK (status IN ('not_contacted', 'in_progress', 'dismissed', 'done'));

-- The tracker's stop / start (the Boss: «вақтинча stop / start қилиш керак»): one row; none = stopped.
-- Set by the Director's /lidlar (OPS Manager Bot) or the admin's /lidlar (Admin Bot); the other side is told.
CREATE TABLE IF NOT EXISTS lead_handout_switch (
    id       BOOLEAN      PRIMARY KEY DEFAULT TRUE CHECK (id),
    enabled  BOOLEAN      NOT NULL,
    set_by   TEXT         NOT NULL,
    set_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- days_off — days nobody works (a holiday, a company day off), set by the
-- admin with Admin Bot /dam (2026-10-01). On such a day no employee gets the
-- report ask or reminder, cheer messages, a lead or the 15:00 lead question,
-- or task reminders; nobody is counted as not having reported.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS days_off (
    day         DATE         PRIMARY KEY,
    set_by      TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- announcements — a message the admin sends to every employee at once with
-- Admin Bot /elon (e.g. "техник хатолик юз берди"), kept until confirmed so
-- the confirm button carries only its id; sent once.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS announcements (
    id          BIGSERIAL    PRIMARY KEY,
    text        TEXT         NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ,
    sent_count  INTEGER
);

-- ---------------------------------------------------------------------------
-- garmin_leads — leads the Garmin AI bot (emjiemai/Garmin-AI-bot) POSTs to
-- /webhooks/garmin-lead/{secret} (integrations/garmin/leads.py). Before
-- 2026-10-01 they lived only on the bot's ephemeral disk.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS garmin_leads (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id         TEXT         NOT NULL UNIQUE,      -- the bot's own id: a retried POST is stored once
    chat_id         BIGINT       NOT NULL,             -- the customer's Telegram chat with the bot
    urgency         TEXT         NOT NULL CHECK (urgency IN ('now', 'next', 'outage')),
    name            TEXT,
    username        TEXT,
    phone           TEXT,
    phone_added_at  TIMESTAMPTZ,
    product_id      TEXT,
    product_name    TEXT,
    price           NUMERIC,
    budget          TEXT,
    summary         TEXT,
    lang            TEXT,
    source          TEXT,                              -- card / quiz / cmp / cta / telegram
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    received_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_garmin_leads_created ON garmin_leads (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_garmin_leads_chat ON garmin_leads (chat_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- task_drafts — a Director's task held until they confirm who gets it
-- (2026-10-02). Every task (text or file) first comes back to the Director as
-- a list of people with the bot's guess ticked; it goes out only on
-- "Юбориш". ``candidate_ids`` is the list in button order (a button carries
-- only its index); ``selected_ids`` is who is ticked.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_drafts (
    id                         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    director_telegram_user_id  BIGINT       NOT NULL,
    source_message_id          BIGINT,
    raw_message                TEXT         NOT NULL,
    task_summary               TEXT         NOT NULL,
    role_slug                  TEXT,
    due_date                   DATE,
    has_media                  BOOLEAN      NOT NULL DEFAULT false,
    candidate_ids              UUID[]       NOT NULL,
    selected_ids               UUID[]       NOT NULL DEFAULT '{}',
    status                     TEXT         NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'sent', 'cancelled')),
    message_id                 BIGINT,
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT now()
);
ALTER TABLE task_drafts ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 2;  -- P1–P4 picked on the card (10.11)
-- The accepting manager picked on the card (12.11; NULL = the Director himself) and the managers offered when the
-- picker opened (frozen: a button carries only its index into accepter_candidate_ids).
ALTER TABLE task_drafts ADD COLUMN IF NOT EXISTS accepter_employee_id UUID;
ALTER TABLE task_drafts ADD COLUMN IF NOT EXISTS accepter_candidate_ids UUID[] NOT NULL DEFAULT '{}';
ALTER TABLE task_drafts ADD COLUMN IF NOT EXISTS expected_result TEXT;  -- 🎯 the Director's expected result (12.11)
ALTER TABLE task_drafts ADD COLUMN IF NOT EXISTS depends_on UUID;  -- ⏳ the open task it waits for (12.11)
ALTER TABLE task_drafts ADD COLUMN IF NOT EXISTS depends_code TEXT;  -- its T- code, for the card
ALTER TABLE task_drafts ADD COLUMN IF NOT EXISTS helper_for UUID;  -- 🙋 a help task for this blocked task (12.11)

-- ---------------------------------------------------------------------------
-- director_clarifications — OPS Manager Bot's question back when it didn't
-- understand the Director (2026-10-10): the 2–3 meanings it offered as
-- buttons (a button carries only its index). A tap sends that meaning on as
-- if he had written it; once. Holds the Director's words — not in /db.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS director_clarifications (
    id                         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    director_telegram_user_id  BIGINT       NOT NULL,
    question                   TEXT         NOT NULL,
    options                    TEXT[]       NOT NULL,
    chosen                     SMALLINT,
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- The work AI (2026-10-02, integrations/org_bot/ai_chat.py): every
-- employee's free message about their work goes to the AI — the bot no
-- longer carries messages to the Director or between people. The admin can
-- switch it off for one person (ai_chat_off). ``responsibilities`` holds the
-- person's written duties once the owner uploads them; the AI reads them,
-- with the person's own open tasks and leads — nothing of anyone else's.
-- ai_chat_turns is its short memory, per person, never shown to anyone else.
-- (The first version, the same day, was opt-in with an /ai on/off mode.)
-- ---------------------------------------------------------------------------
ALTER TABLE employees DROP COLUMN IF EXISTS ai_chat;
ALTER TABLE employees DROP COLUMN IF EXISTS ai_chat_until;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS ai_chat_off BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS responsibilities TEXT;

CREATE TABLE IF NOT EXISTS ai_chat_turns (
    id                BIGSERIAL    PRIMARY KEY,
    telegram_user_id  BIGINT       NOT NULL,
    role              TEXT         NOT NULL CHECK (role IN ('employee', 'assistant')),
    content           TEXT         NOT NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_chat_turns_recent ON ai_chat_turns (telegram_user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Employees' files and today's report (2026-10-02). A photo, video or file an
-- employee sends is held in employee_files until they say what it's for:
-- today's report or "to the Director" — then it is forwarded to the Director
-- as it is (the AI never reads it). Today's report can be changed or deleted
-- with /hisobot: editing_at marks "the next message is the new text";
-- media_count counts files sent as part of it.
-- ---------------------------------------------------------------------------
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS editing_at TIMESTAMPTZ;
-- Some people find the cheer messages irritating: the admin can switch them
-- off per person on the /xodimlar card (2026-10-02).
ALTER TABLE employees ADD COLUMN IF NOT EXISTS cheer_off BOOLEAN NOT NULL DEFAULT false;
-- Their own week (2026-10-17, the owner: Garmin shop staff work shifts, weekdays off too): seven
-- '1'/'0' for Monday..Sunday — the days they are asked for a report. NULL = Monday–Friday plus the
-- Saturday / Sunday switches. Set with seven buttons on /xodimlar (kpi.OWN_SCHEDULE_ROLES).
ALTER TABLE employees ADD COLUMN IF NOT EXISTS work_days TEXT;
ALTER TABLE employees DROP CONSTRAINT IF EXISTS employees_work_days_check;
ALTER TABLE employees ADD CONSTRAINT employees_work_days_check CHECK (work_days IS NULL OR work_days ~ '^[01]{7}$');
-- Daily reports off for one person (2026-10-06, /xodimlar → 📝): never asked
-- at 16:00 or reminded at 17:00, so no row is opened — nothing counts as
-- missed in the brief, the Friday scorecard or the KPI.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS reports_off BOOLEAN NOT NULL DEFAULT false;
-- Company data through the bot for accounting and finance (2026-10-10, the
-- Director's instruction; integrations/org_bot/data_access.py): which systems
-- this person may ask about — 'sap', '1c', 'billz'. Set by the admin on the
-- /xodimlar card, only for the Бухгалтерия and Молия roles; every change is
-- in employee_changes and shown to the Director, who can close it.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS data_access TEXT[] NOT NULL DEFAULT '{}';
-- The language the AI answers this person in (2026-10-10): 'uz' (Uzbek
-- Cyrillic, the rule) or 'ru' — set by the admin, never guessed.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS lang TEXT NOT NULL DEFAULT 'uz';
-- 2026-11-04: Telegram said "bot was blocked by the user" (OPS Manager Bot); cleared when they write again.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS bot_blocked_at TIMESTAMPTZ;
-- A department manager's deputy (2026-11-12, Admin Bot /xodimlar 👥, TEAM_OF manager roles only; the deputy is one of
-- his team): gets the escalation ladder's step when the manager isn't at work that day. The Director is told of
-- every change. Names only — structure, no figures.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS deputy_employee_id UUID REFERENCES employees(id) ON DELETE SET NULL;
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS media_count INTEGER NOT NULL DEFAULT 0;
-- Reports close at 22:00 (2026-10-15, the Director); an ask still open then gets minus KPI
-- points (REPORT_MISS_PENALTY) and the person is told — once (penalized_at).
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS penalty_points INTEGER NOT NULL DEFAULT 0;
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS penalized_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS employee_files (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id  BIGINT       NOT NULL,
    message_id        BIGINT       NOT NULL,
    kind              TEXT         NOT NULL,
    caption           TEXT,
    purpose           TEXT         CHECK (purpose IN ('report', 'director', 'cancelled')),
    sent_count        INTEGER,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- "Эркин график" (2026-10-03): most staff come when work needs them or the
-- Director calls, so Verifix's fixed start time made them "late" or "absent"
-- every day. People marked here (Admin Bot /grafik, by their Verifix id) are
-- never judged late or absent — the brief only counts them.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attendance_flexible (
    verifix_id  TEXT         PRIMARY KEY,
    name        TEXT         NOT NULL,
    set_by      TEXT,
    set_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- cheques (2026-10-14): a cheque photo the Director sent, as the AI read it
-- (org_bot/cheques.py). Sent to accounting when every detail is there; asked
-- about otherwise. The fiscal keys find a cheque sent twice. Company data —
-- never in the /db viewer's technical tables.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cheques (
    id                         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    director_telegram_user_id  BIGINT       NOT NULL,
    source_message_id          BIGINT       NOT NULL,
    caption                    TEXT,
    fields                     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    problems                   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    fiscal_sign                TEXT,
    fiscal_module              TEXT,
    rrn                        TEXT,
    status                     TEXT         NOT NULL DEFAULT 'asked'
                               CHECK (status IN ('asked', 'sent', 'cancelled', 'as_task', 'no_recipient')),
    sent_count                 INTEGER      NOT NULL DEFAULT 0,
    resolved_at                TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cheques_fiscal ON cheques (fiscal_module, fiscal_sign) WHERE fiscal_sign IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cheques_rrn ON cheques (rrn) WHERE rrn IS NOT NULL;

-- Garmin competitors (2026-10-19, integrations/garmin/competitors.py, agents/garmin-competitors):
-- public OLX listings and Uzbek web pages that sell Garmin — what is new and what got cheaper since the
-- last check. A private seller is never named (no name, no phone): `seller` is a shop's name or a site.
CREATE TABLE IF NOT EXISTS garmin_competitor_offers (
    source          TEXT         NOT NULL CHECK (source IN ('olx', 'web')),
    external_id     TEXT         NOT NULL,             -- OLX listing id / the page's address
    title           TEXT         NOT NULL,
    model_key       TEXT,                              -- «fenix 8», «forerunner 970» …
    state           TEXT,                              -- new / used (OLX); NULL when not said
    price_uzs       BIGINT,
    prev_price_uzs  BIGINT,                            -- the price before it last changed
    seller          TEXT,
    is_business     BOOLEAN      NOT NULL DEFAULT false,
    url             TEXT         NOT NULL,
    posted_on       DATE,
    first_seen      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_garmin_competitor_offers_seen ON garmin_competitor_offers (last_seen DESC);

-- Competitor shops' contacts (2026-10-20, integrations/garmin/competitor_shops.py): what each SHOP publishes
-- for customers — its OLX shop page, its site, its Telegram channel (and its Instagram found by name).
-- Shops only: OLX business accounts, sites, channels; a private person is never looked up or stored.
CREATE TABLE IF NOT EXISTS garmin_competitor_shops (
    key           TEXT         PRIMARY KEY,           -- olx:<account> / site:<domain> / tg:<channel> / ig:<handle>
    name          TEXT,
    phones        TEXT[]       NOT NULL DEFAULT '{}',
    instagram     TEXT,
    telegram      TEXT,
    website       TEXT,
    address       TEXT,
    olx_page      TEXT,
    first_seen    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    checked_at    TIMESTAMPTZ,                        -- their pages read
    searched_at   TIMESTAMPTZ                         -- Google asked for their Instagram
);

-- Files as the daily report (2026-10-22, the owner): a voice / video becomes report text (report_media.py);
-- a photo / document is kept and sent from here when someone asks for that report. file_id = Telegram's
-- (the OPS Manager bot can send it again); report_id = the report it belongs to; text = the voice / video as text.
ALTER TABLE employee_files ADD COLUMN IF NOT EXISTS file_id TEXT;
ALTER TABLE employee_files ADD COLUMN IF NOT EXISTS mime TEXT;
ALTER TABLE employee_files ADD COLUMN IF NOT EXISTS file_size BIGINT;
ALTER TABLE employee_files ADD COLUMN IF NOT EXISTS report_id UUID REFERENCES daily_reports(id) ON DELETE SET NULL;
ALTER TABLE employee_files ADD COLUMN IF NOT EXISTS text TEXT;
CREATE INDEX IF NOT EXISTS idx_employee_files_report ON employee_files (report_id) WHERE report_id IS NOT NULL;
-- A report the bot put together at 22:00 from what the person sent that day but never confirmed as the report
-- (the button forgotten — human error, the owner 2026-10-22): counted as sent, no minus, marked.
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS auto_built BOOLEAN NOT NULL DEFAULT false;

-- A person's own start time (2026-10-22, the owner: "some employees come at 10 a.m."): Admin Bot /grafik,
-- by Verifix id. "Late" is counted from this time instead of Verifix's schedule start.
CREATE TABLE IF NOT EXISTS attendance_starts (
    verifix_id  TEXT         PRIMARY KEY,
    name        TEXT         NOT NULL,
    start_time  TIME         NOT NULL,
    set_by      TEXT,
    set_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Explanations (2026-10-22, the Boss: «AI сўрасин, тушунтириш хати ёзиб беришсин»): each morning the bot asks
-- whoever Verifix shows absent yesterday (with the days in a row) or whose report didn't come; the answer goes
-- to the Boss (and the Garmin manager for his team) as a card. One ask per person, kind and day.
CREATE TABLE IF NOT EXISTS explanations (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id        UUID         NOT NULL REFERENCES employees(id),
    telegram_user_id   BIGINT       NOT NULL,
    kind               TEXT         NOT NULL CHECK (kind IN ('absent', 'report')),
    asked_for          DATE         NOT NULL,          -- the day it is about (yesterday at the ask)
    days               DATE[]       NOT NULL,          -- every day it covers (absent several days in a row)
    prompt_message_id  BIGINT,
    asked_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    answer             TEXT,
    answered_at        TIMESTAMPTZ,
    delivered          INTEGER      NOT NULL DEFAULT 0,
    CONSTRAINT uq_explanation UNIQUE (employee_id, kind, asked_for)
);
CREATE INDEX IF NOT EXISTS idx_explanations_open ON explanations (telegram_user_id, asked_at DESC) WHERE answered_at IS NULL;
-- explanation: 2026-10-22; task (a task's proof): 2026-11-05. ONE definition only (2026-11-11: an older stricter
-- copy right above it failed once rows with 'task' existed, and the whole start-up schema rolled back).
ALTER TABLE employee_files DROP CONSTRAINT IF EXISTS employee_files_purpose_check;
ALTER TABLE employee_files ADD CONSTRAINT employee_files_purpose_check
    CHECK (purpose IN ('report', 'director', 'cancelled', 'explanation', 'task'));

-- Report bonus (2026-10-22, the owner): each morning a Garmin сотув person whose report of yesterday they sent
-- themselves gets +REPORT_BONUS_POINTS (1) KPI; a missed one is told politely no point was given. bonus_at = told, once.
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS bonus_points INTEGER NOT NULL DEFAULT 0;
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS bonus_at TIMESTAMPTZ;

-- Excused days (2026-10-23, the Boss): the Director (anyone) or the Garmin manager (his team) marks a person's day
-- as having a good reason — the explanation card's ⬜ button or /sababli. The day turns grey: Verifix's absent / late
-- becomes excused, an unsent report's row is taken out (its minus with it; kept in `report_row` so ↩ puts it back).
CREATE TABLE IF NOT EXISTS excused_days (
    employee_id  UUID         NOT NULL REFERENCES employees(id),
    day          DATE         NOT NULL,
    reason       TEXT,
    set_by       BIGINT       NOT NULL,
    set_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    report_row   JSONB,
    PRIMARY KEY (employee_id, day)
);

-- Late reports (2026-10-23, the Boss): a report not sent by 22:00 can be sent the next day from the morning ask's
-- button; it goes to the Director / the Garmin manager (his team) to approve — approved, the day turns green (its minus
-- removed); declined, it stays red. No report bonus for it.
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS late_awaiting_at TIMESTAMPTZ;
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS late_content TEXT;
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS late_sent_at TIMESTAMPTZ;
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS late_decision TEXT;
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS late_decided_by BIGINT;
ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS late_decided_at TIMESTAMPTZ;

-- 2026-10-24 (the owner): an excused day keeps its report row — status 'excused' (grey, never counted, its minus 0)
-- instead of the row being taken out, so nothing is lost and the admin's report sheet can turn a day green / red /
-- grey and back. Every list and count of reports leaves 'excused' rows out.
ALTER TABLE daily_reports DROP CONSTRAINT IF EXISTS daily_reports_status_check;
ALTER TABLE daily_reports ADD CONSTRAINT daily_reports_status_check CHECK (status IN ('asked', 'submitted', 'excused'));

-- Days Verifix didn't record (2026-10-24, the owner: "Verifix didn't work, employees blamed the bot"): IT marks the
-- day on Admin Bot /verifix — nobody is judged absent or late that day, the «why weren't you at work?» asks about it
-- are withdrawn with an apology, the Director is told. A day with almost no arrivals is treated so on its own.
CREATE TABLE IF NOT EXISTS verifix_bad_days (
    day     DATE         PRIMARY KEY,
    set_by  TEXT         NOT NULL,
    set_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- The admin's report sheet (2026-10-24, Admin Bot /xodimlar → 📊): every day the admin turned green / red / grey,
-- for the Director's summary (told once: told_at).
CREATE TABLE IF NOT EXISTS report_cell_edits (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id  UUID         NOT NULL REFERENCES employees(id),
    day          DATE         NOT NULL,
    before       TEXT         NOT NULL,
    after        TEXT         NOT NULL,
    set_by       TEXT         NOT NULL,
    set_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    told_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_report_cell_edits_untold ON report_cell_edits (set_at) WHERE told_at IS NULL;

-- One morning message per employee (2026-10-24, the owner: "it might ask multiple questions at once"): the 08:00
-- agents (report bonus, explanations, task reminders) leave notes here; agents/morning-notes sends each person one
-- message — short lines, at most one question last.
CREATE TABLE IF NOT EXISTS morning_notes (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    day               DATE         NOT NULL,
    employee_id       UUID         NOT NULL REFERENCES employees(id),
    telegram_user_id  BIGINT       NOT NULL,
    kind              TEXT         NOT NULL CHECK (kind IN ('bonus', 'task', 'ask')),
    detail            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    sent_at           TIMESTAMPTZ,
    message_id        BIGINT
);
CREATE INDEX IF NOT EXISTS idx_morning_notes_unsent ON morning_notes (day) WHERE sent_at IS NULL;

-- Lead groups (2026-10-26, the owner): the Telegram group each business's leads go to, one message per lead —
-- set by the admin / the Director typing /lidlar_garmin or /lidlar_primus in the group (lead_groups.py).
CREATE TABLE IF NOT EXISTS lead_groups (
    business  TEXT         PRIMARY KEY CHECK (business IN ('garmin', 'primus')),
    chat_id   BIGINT       NOT NULL,
    title     TEXT,
    set_by    BIGINT       NOT NULL,
    set_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Our own lead search index (2026-10-30, the owner: «build a search engine like Tavily and SerpApi»;
-- integrations/search/crawler.py, spec 04): Uzbek news RSS, public Telegram channels and Google News RSS,
-- collected every 3 hours in the day and before the 08:00 Lead Agent; searched with full-text (object word
-- AND signal word). Kept 60 days. crawl_runs: one row per crawl (counts only).
CREATE TABLE IF NOT EXISTS crawl_items (
    id            BIGSERIAL    PRIMARY KEY,
    url           TEXT         NOT NULL UNIQUE,
    source        TEXT         NOT NULL,
    title         TEXT         NOT NULL,
    body          TEXT,
    published_at  TIMESTAMPTZ,
    fetched_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    tsv           TSVECTOR     GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(body, ''))) STORED
);
CREATE INDEX IF NOT EXISTS idx_crawl_items_tsv ON crawl_items USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_crawl_items_fetched ON crawl_items (fetched_at DESC);
CREATE TABLE IF NOT EXISTS crawl_runs (
    id          BIGSERIAL    PRIMARY KEY,
    started_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    sources     INTEGER      NOT NULL,
    failed      INTEGER      NOT NULL,
    items       INTEGER      NOT NULL,
    new_items   INTEGER      NOT NULL
);

-- The 09:00 discrepancy report for accounting (2026-11-02, T-2026-11-0048, integrations/reconcile/): one row per
-- day it went out — the API's 5-minute round claims the day, so it is sent once.
CREATE TABLE IF NOT EXISTS reconcile_runs (
    day      DATE         PRIMARY KEY,
    sent_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- The duties register (2026-11-02, the Boss's T-0051 / T-0054 / T-0055; integrations/org_bot/duties.py): who does
-- what, how often, to which target — read every morning from the Google Sheet «Вазифалар» (or the Boss's example of
-- 02.11 until the sheet is set). row_key = person + duty + how often; every change kept in duty_changes. A person's
-- active duties become employees.responsibilities (the work AI reads it). monthly_plans: the «Режалар» tab.
CREATE TABLE IF NOT EXISTS duties (
    id           BIGSERIAL    PRIMARY KEY,
    row_key      TEXT         NOT NULL UNIQUE,
    person       TEXT         NOT NULL,
    position     TEXT         NOT NULL DEFAULT '',
    title        TEXT         NOT NULL,
    cadence      TEXT         NOT NULL CHECK (cadence IN ('daily', 'weekly', 'monthly', 'deadline', 'rule', 'as_needed')),
    day          TEXT         NOT NULL DEFAULT '',
    time         TEXT         NOT NULL DEFAULT '',
    target       TEXT         NOT NULL DEFAULT '',
    unit         TEXT         NOT NULL DEFAULT '',
    proof        TEXT         NOT NULL DEFAULT '',
    verify       TEXT         NOT NULL DEFAULT '',
    note         TEXT         NOT NULL DEFAULT '',
    employee_id  UUID         REFERENCES employees (id),
    active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_duties_employee ON duties (employee_id) WHERE active;
CREATE TABLE IF NOT EXISTS duty_changes (
    id       BIGSERIAL    PRIMARY KEY,
    at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    row_key  TEXT         NOT NULL,
    change   TEXT         NOT NULL CHECK (change IN ('added', 'changed', 'removed', 'synced')),
    before   JSONB,
    after    JSONB
);
CREATE TABLE IF NOT EXISTS monthly_plans (
    direction   TEXT         PRIMARY KEY,
    amount_usd  TEXT         NOT NULL DEFAULT '',
    owner       TEXT         NOT NULL DEFAULT '',
    note        TEXT         NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Daily tasks from the duties register (2026-11-02, week 2; integrations/org_bot/duty_tasks.py): one row per duty and
-- period (a day; a weekday; the week; the month; a deadline's last 7 days), answered on one card per person —
-- ✅ (with the number when the duty has a target), ⛔ (with the reason), 👎 «фойдасиз». duty_cards: the cards sent;
-- duty_runs: each step once a day; duty_switch: on / off (no row = off).
CREATE TABLE IF NOT EXISTS duty_tasks (
    id            BIGSERIAL    PRIMARY KEY,
    duty_id       BIGINT       NOT NULL REFERENCES duties (id),
    employee_id   UUID         NOT NULL REFERENCES employees (id),
    period_start  DATE         NOT NULL,
    period_end    DATE         NOT NULL,
    cadence       TEXT         NOT NULL,
    title         TEXT         NOT NULL,
    target        TEXT         NOT NULL DEFAULT '',
    unit          TEXT         NOT NULL DEFAULT '',
    status        TEXT         NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'not_done', 'missed')),
    value         NUMERIC,
    reason        TEXT,
    useless       BOOLEAN      NOT NULL DEFAULT FALSE,
    useless_at    TIMESTAMPTZ,
    answered_at   TIMESTAMPTZ,
    asked_kind    TEXT         CHECK (asked_kind IN ('value', 'reason')),
    asked_at      TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (duty_id, period_start)
);
CREATE INDEX IF NOT EXISTS idx_duty_tasks_person ON duty_tasks (employee_id, period_end);
CREATE TABLE IF NOT EXISTS duty_cards (
    employee_id  UUID         NOT NULL REFERENCES employees (id),
    day          DATE         NOT NULL,
    kind         TEXT         NOT NULL CHECK (kind IN ('morning', 'evening')),
    message_id   BIGINT,
    sent_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (employee_id, day, kind)
);
CREATE TABLE IF NOT EXISTS duty_runs (
    day   DATE  NOT NULL,
    kind  TEXT  NOT NULL,
    at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS duty_switch (
    id       BOOLEAN      PRIMARY KEY DEFAULT TRUE CHECK (id),
    enabled  BOOLEAN      NOT NULL,
    set_by   TEXT         NOT NULL,
    set_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Week 3 (2026-11-02): plan vs fact and the system's check (integrations/org_bot/plan_facts.py, duty_review.py).
-- sales_facts: every pushed SAP invoice line kept (SAP pushes 14 days; the month adds up here), dollars from the
-- document's gross total split over its lines; product group from the item master; the salesperson.
CREATE TABLE IF NOT EXISTS sales_facts (
    obj_type    TEXT     NOT NULL,
    doc_entry   TEXT     NOT NULL,
    line_num    INTEGER  NOT NULL,
    day         DATE     NOT NULL,
    slp_code    TEXT     NOT NULL DEFAULT '',
    item_code   TEXT     NOT NULL DEFAULT '',
    item_group  TEXT     NOT NULL DEFAULT '',
    wh          TEXT     NOT NULL DEFAULT '',
    amount_usd  NUMERIC  NOT NULL DEFAULT 0,
    amount_som  NUMERIC  NOT NULL DEFAULT 0,
    PRIMARY KEY (obj_type, doc_entry, line_num)
);
CREATE INDEX IF NOT EXISTS idx_sales_facts_day ON sales_facts (day);
ALTER TABLE duty_tasks ADD COLUMN IF NOT EXISTS fact NUMERIC;            -- the system's own figure (SAP / Billz)
ALTER TABLE duty_tasks ADD COLUMN IF NOT EXISTS fact_source TEXT;
ALTER TABLE duty_tasks ADD COLUMN IF NOT EXISTS fact_at TIMESTAMPTZ;
ALTER TABLE duty_tasks ADD COLUMN IF NOT EXISTS mismatch BOOLEAN NOT NULL DEFAULT FALSE;  -- typed number > fact + 10 %
-- The Friday review's suggestions to change the register: chosen by rules, written by the AI, decided by the Director.
CREATE TABLE IF NOT EXISTS duty_suggestions (
    id          BIGSERIAL    PRIMARY KEY,
    week        DATE         NOT NULL,
    duty_id     BIGINT,
    person      TEXT         NOT NULL,
    title       TEXT         NOT NULL,
    change      TEXT         NOT NULL,
    why         TEXT         NOT NULL,
    status      TEXT         NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    decided_by  TEXT,
    decided_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- The leads sheet kept readable (2026-11-03, lead_sheet.py): leads added before DeepSeek's first run (31.10) moved to
-- the sheet's «Архив» tab and out of the hand-out queue (never deleted).
ALTER TABLE leads ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE duty_tasks ADD COLUMN IF NOT EXISTS asked_message_id BIGINT;  -- the «нечта?» / «нима сабаб?» question (03.11)

-- ============================================================================
-- Schema-first SQL for the analyst (2026-11-09, the owner: "send the AI the structure, not the data, and fetch only
-- the rows it asks for"). Typed, question-shaped views over the latest SAP push and the bot's own records, read by
-- the analyst's query_db tool (integrations/org_bot/sql_catalog.py, sql_guard.py, db.fetch_analyst) as a NOLOGIN
-- role that may SELECT these views and nothing else, in a READ ONLY transaction with a 4 s timeout. Amounts keep their
-- unit in the column name: _usd = SAP's local currency (USD), _som = its system currency (UZS), _doc = the document's
-- own currency (doc_currency). SAP dates arrive as 'YYYY-MM-DD HH:MM:SS.nnnnnnnnn' text — the date part is taken,
-- never a timestamp cast (it would shift the day). Plain views: ≤ 2k rows per kind, milliseconds.
-- ============================================================================
CREATE SCHEMA IF NOT EXISTS analyst;
-- The views are rebuilt on every start (nothing depends on them; the grants below are applied again), so a changed
-- column never stops the start-up — CREATE OR REPLACE VIEW refuses renamed or reordered columns.
DO $$
DECLARE v record;
BEGIN
    FOR v IN SELECT table_name FROM information_schema.views WHERE table_schema = 'analyst' LOOP
        EXECUTE format('DROP VIEW IF EXISTS analyst.%I CASCADE', v.table_name);
    END LOOP;
END $$;

-- A number / a date from a SAP text field, or NULL when it isn't one (a stray value must not fail the whole query).
CREATE OR REPLACE FUNCTION analyst.num(v text) RETURNS numeric LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN v ~ '^\s*-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?\s*$' THEN v::numeric END
$$;
CREATE OR REPLACE FUNCTION analyst.day(v text) RETURNS date LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN v ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN left(v, 10)::date END
$$;
-- A name in one Latin skeleton, so «Хилтон», «Hilton» and «Xilton» match (2026-11-09): Cyrillic → Latin, Uzbek-Latin
-- x/kh → h, and the letters a Russian keyboard swaps folded together (қ = к = q, ў = у = o', дж = ж, word-initial
-- ye = е: «Ёрқинов» = «Ёркинов», «Мирпўлатов» = «Мирпулатов», «Ходжиев» = «Хожиев» — as plan_facts.fold).
-- Lossy on purpose; mirrored by integrations/common/translit.skeleton (selfcheck keeps the two identical).
CREATE OR REPLACE FUNCTION analyst.name_key(v text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT btrim(regexp_replace(regexp_replace(regexp_replace(
        translate(
            replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(
                lower(coalesce(v, '')),
                'kh', 'h'), 'dj', 'j'), 'o''', 'u'), 'oʻ', 'u'), 'o‘', 'u'), 'o’', 'u'), 'дж', 'j'),
                'ш', 'sh'), 'щ', 'sh'), 'ч', 'ch'), 'ё', 'yo'), 'ю', 'yu'), 'я', 'ya'), 'ц', 'ts'),
            'абвгдежзийклмнопрстуфхҳыэўғқxqъь', 'abvgdejziyklmnoprstufhhieugkhk'),
        '[^a-z0-9 ]', '', 'g'), '(^| )ye', '\1e', 'g'), '\s+', ' ', 'g'))
$$;

CREATE OR REPLACE VIEW analyst.sap_sales_docs AS
SELECT coalesce(s.raw->>'ObjType', '13') AS obj_type,
       CASE WHEN s.raw->>'ObjType' = '14' THEN 'credit_note' ELSE 'invoice' END AS doc_kind,
       CASE WHEN s.raw->>'ObjType' = '14' THEN -1 ELSE 1 END AS sign,
       analyst.num(s.raw->>'DocEntry')::bigint AS doc_entry,
       analyst.num(s.raw->>'DocNum')::bigint AS doc_num,
       analyst.day(s.raw->>'DocDate') AS doc_date,
       analyst.day(s.raw->>'DocDueDate') AS due_date,
       analyst.day(s.raw->>'CreateDate') AS created_on,
       analyst.num(s.raw->>'CreateTS')::int AS create_ts,
       s.raw->>'CardCode' AS card_code,
       s.raw->>'CardName' AS card_name,
       analyst.name_key(s.raw->>'CardName') AS name_key,
       analyst.num(s.raw->>'SlpCode')::int AS slp_code,
       s.raw->>'SlpName' AS slp_name,
       s.raw->>'UserName' AS user_name,
       s.raw->>'DocCur' AS doc_currency,
       analyst.num(s.raw->>'DocTotal') AS total_usd,
       analyst.num(s.raw->>'DocTotalSy') AS total_som,
       analyst.num(s.raw->>'DocTotalFC') AS total_doc,
       CASE WHEN s.raw->>'ObjType' = '14' THEN -1 ELSE 1 END * analyst.num(s.raw->>'DocTotalSy') AS signed_total_som,
       CASE WHEN s.raw->>'DocStatus' = 'O' THEN 'open' ELSE 'closed' END AS doc_status,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'sales';

CREATE OR REPLACE VIEW analyst.sap_sales_lines AS
SELECT coalesce(s.raw->>'ObjType', '13') AS obj_type,
       CASE WHEN s.raw->>'ObjType' = '14' THEN 'credit_note' ELSE 'invoice' END AS doc_kind,
       CASE WHEN s.raw->>'ObjType' = '14' THEN -1 ELSE 1 END AS sign,
       analyst.num(s.raw->>'DocEntry')::bigint AS doc_entry,
       analyst.num(s.raw->>'DocNum')::bigint AS doc_num,
       analyst.num(s.raw->>'LineNum')::int AS line_num,
       analyst.day(s.raw->>'DocDate') AS doc_date,
       analyst.day(s.raw->>'CreateDate') AS created_on,
       s.raw->>'CardCode' AS card_code,
       s.raw->>'CardName' AS card_name,
       analyst.name_key(s.raw->>'CardName') AS name_key,
       analyst.num(s.raw->>'SlpCode')::int AS slp_code,
       s.raw->>'SlpName' AS slp_name,
       s.raw->>'UserName' AS user_name,
       s.raw->>'ItemCode' AS item_code,
       s.raw->>'Dscription' AS item_name,
       analyst.name_key(s.raw->>'Dscription') AS item_key,
       s.raw->>'CodeBars' AS barcode,
       s.raw->>'WhsCode' AS whs_code,
       analyst.num(s.raw->>'Quantity') AS quantity,
       CASE WHEN s.raw->>'ObjType' = '14' THEN -1 ELSE 1 END * analyst.num(s.raw->>'Quantity') AS signed_quantity,
       analyst.num(s.raw->>'Price') AS price,
       s.raw->>'LineCurrency' AS line_currency,
       analyst.num(s.raw->>'LineTotal') AS line_usd,
       analyst.num(s.raw->>'GTotalSC') AS line_som_gross,
       analyst.num(s.raw->>'TotalSumSy') AS line_som_net,
       CASE WHEN s.raw->>'ObjType' = '14' THEN -1 ELSE 1 END * analyst.num(s.raw->>'GTotalSC') AS signed_line_som_gross,
       analyst.num(s.raw->>'DocTotalSy') AS doc_total_som,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'sales_lines';

CREATE OR REPLACE VIEW analyst.sap_payments_in AS
SELECT analyst.num(s.raw->>'DocEntry')::bigint AS doc_entry,
       analyst.num(s.raw->>'DocNum')::bigint AS doc_num,
       analyst.day(s.raw->>'DocDate') AS doc_date,
       s.raw->>'CardCode' AS card_code,
       s.raw->>'CardName' AS card_name,
       analyst.name_key(s.raw->>'CardName') AS name_key,
       s.raw->>'DocCurr' AS doc_currency,
       analyst.num(s.raw->>'DocTotal') AS total_usd,
       analyst.num(s.raw->>'DocTotalSy') AS total_som,
       analyst.num(s.raw->>'DocTotalFC') AS total_doc,
       analyst.num(s.raw->>'CashSum') AS cash_usd,
       analyst.num(s.raw->>'TrsfrSum') AS transfer_usd,
       analyst.num(s.raw->>'CheckSum') AS cheque_usd,
       analyst.num(s.raw->>'CreditSum') AS card_usd,
       s.raw->>'Comments' AS comments,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'payments';

CREATE OR REPLACE VIEW analyst.sap_payments_out AS
SELECT analyst.num(s.raw->>'DocEntry')::bigint AS doc_entry,
       analyst.num(s.raw->>'DocNum')::bigint AS doc_num,
       analyst.day(s.raw->>'DocDate') AS doc_date,
       s.raw->>'CardCode' AS card_code,
       s.raw->>'CardName' AS card_name,
       analyst.name_key(s.raw->>'CardName') AS name_key,
       s.raw->>'DocCurr' AS doc_currency,
       analyst.num(s.raw->>'DocTotal') AS total_usd,
       analyst.num(s.raw->>'DocTotalSy') AS total_som,
       analyst.num(s.raw->>'DocTotalFC') AS total_doc,
       analyst.num(s.raw->>'CashSum') AS cash_usd,
       analyst.num(s.raw->>'TrsfrSum') AS transfer_usd,
       analyst.num(s.raw->>'CheckSum') AS cheque_usd,
       analyst.num(s.raw->>'CreditSum') AS card_usd,
       s.raw->>'Comments' AS comments,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'payments_out';

CREATE OR REPLACE VIEW analyst.sap_ap_open AS
SELECT analyst.num(s.raw->>'DocEntry')::bigint AS doc_entry,
       analyst.num(s.raw->>'DocNum')::bigint AS doc_num,
       s.raw->>'NumAtCard' AS supplier_ref,
       analyst.day(s.raw->>'DocDate') AS doc_date,
       analyst.day(s.raw->>'DocDueDate') AS due_date,
       s.raw->>'CardCode' AS card_code,
       s.raw->>'CardName' AS card_name,
       analyst.name_key(s.raw->>'CardName') AS name_key,
       s.raw->>'LicTradNum' AS inn,
       s.raw->>'DocCur' AS doc_currency,
       analyst.num(s.raw->>'DocTotal') AS total_usd,
       analyst.num(s.raw->>'PaidToDate') AS paid_usd,
       analyst.num(s.raw->>'DocTotal') - coalesce(analyst.num(s.raw->>'PaidToDate'), 0) AS open_usd,
       analyst.num(s.raw->>'DocTotalSy') AS total_som,
       analyst.num(s.raw->>'PaidSys') AS paid_som,
       analyst.num(s.raw->>'DocTotalSy') - coalesce(analyst.num(s.raw->>'PaidSys'), 0) AS open_som,
       analyst.num(s.raw->>'DocTotalFC') AS total_doc,
       analyst.num(s.raw->>'PaidFC') AS paid_doc,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'ap_open';

CREATE OR REPLACE VIEW analyst.sap_supplier_balances AS
SELECT s.raw->>'CardCode' AS card_code,
       s.raw->>'CardName' AS card_name,
       analyst.name_key(s.raw->>'CardName') AS name_key,
       s.raw->>'LicTradNum' AS inn,
       s.raw->>'CardType' AS card_type,
       s.raw->>'Currency' AS currency,
       analyst.num(s.raw->>'Balance') AS balance_usd,
       analyst.num(s.raw->>'BalanceSys') AS balance_som,
       analyst.num(s.raw->>'BalanceFC') AS balance_fc,
       analyst.num(s.raw->>'Balance') < 0 AS we_owe,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'supplier_balances';

CREATE OR REPLACE VIEW analyst.sap_customers AS
SELECT s.raw->>'CardCode' AS card_code,
       s.raw->>'CardName' AS card_name,
       analyst.name_key(s.raw->>'CardName') AS name_key,
       s.raw->>'LicTradNum' AS inn,
       analyst.num(s.raw->>'GroupCode')::int AS group_code,
       s.raw->>'GroupName' AS group_name,
       s.raw->>'Currency' AS currency,
       analyst.num(s.raw->>'Balance') AS balance_usd,
       analyst.num(s.raw->>'BalanceSys') AS balance_som,
       analyst.num(s.raw->>'CreditLine') AS credit_line,
       analyst.num(s.raw->>'SlpCode')::int AS slp_code,
       s.raw->>'City' AS city,
       analyst.day(s.raw->>'CreateDate') AS created_on,
       analyst.day(s.raw->>'UpdateDate') AS updated_on,
       coalesce(s.raw->>'validFor', 'Y') <> 'N' AND coalesce(s.raw->>'frozenFor', 'N') <> 'Y' AS active,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'customers';

CREATE OR REPLACE VIEW analyst.sap_products AS
SELECT s.raw->>'ItemCode' AS item_code,
       s.raw->>'ItemName' AS item_name,
       analyst.name_key(s.raw->>'ItemName') AS item_key,
       analyst.num(s.raw->>'ItmsGrpCod')::int AS group_code,
       s.raw->>'ItmsGrpNam' AS group_name,
       s.raw->>'CodeBars' AS barcode,
       s.raw->>'InvntryUom' AS uom,
       analyst.num(s.raw->>'OnHand') AS on_hand,
       analyst.num(s.raw->>'IsCommited') AS committed,
       analyst.num(s.raw->>'OnOrder') AS on_order,
       analyst.num(s.raw->>'MinLevel') AS min_level,
       analyst.num(s.raw->>'MaxLevel') AS max_level,
       analyst.num(s.raw->>'AvgPrice') AS avg_price_usd,
       analyst.num(s.raw->>'LastPurPrc') AS last_purchase_price,
       analyst.day(s.raw->>'LastPurDat') AS last_purchase_date,
       s.raw->>'InvntItem' = 'Y' AS inventory_item,
       s.raw->>'SellItem' = 'Y' AS sellable,
       s.raw->>'PrchseItem' = 'Y' AS purchasable,
       coalesce(s.raw->>'validFor', 'Y') <> 'N' AND coalesce(s.raw->>'frozenFor', 'N') <> 'Y' AS active,
       (s.raw->>'ItemName' ~* '^\s*dummy' OR s.raw->>'ItemCode' ~* '^[0-9]{3}-M[0-9]') AS is_dummy,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'products';

CREATE OR REPLACE VIEW analyst.sap_stock AS
SELECT s.raw->>'ItemCode' AS item_code,
       s.raw->>'ItemName' AS item_name,
       analyst.name_key(s.raw->>'ItemName') AS item_key,
       s.raw->>'WhsCode' AS whs_code,
       s.raw->>'WhsName' AS whs_name,
       analyst.num(s.raw->>'OnHand') AS on_hand,
       analyst.num(s.raw->>'AvgPrice') AS avg_price_usd,
       analyst.num(s.raw->>'StockValue') AS stock_value_usd,
       (s.raw->>'ItemName' ~* '^\s*dummy' OR s.raw->>'ItemCode' ~* '^[0-9]{3}-M[0-9]') AS is_dummy,
       coalesce(lower(s.raw->>'WhsName') ~ '(faulty|брак|brak|nosoz|носоз|defect)', false) AS is_faulty,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'stock_items';

CREATE OR REPLACE VIEW analyst.sap_stock_by_warehouse AS
SELECT s.raw->>'WhsCode' AS whs_code,
       s.raw->>'WhsName' AS whs_name,
       analyst.num(s.raw->>'Items')::int AS items,
       analyst.num(s.raw->>'OnHand') AS on_hand,
       analyst.num(s.raw->>'StockValue') AS stock_value_usd,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'stock_value';

CREATE OR REPLACE VIEW analyst.sap_item_sales_90d AS
SELECT s.raw->>'ItemCode' AS item_code,
       s.raw->>'WhsCode' AS whs_code,
       analyst.num(s.raw->>'Days')::int AS days,
       analyst.num(s.raw->>'Quantity') AS quantity_sold,
       analyst.num(s.raw->>'LineTotal') AS line_usd,
       analyst.day(s.raw->>'LastSold') AS last_sold,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'item_sales_90d';

CREATE OR REPLACE VIEW analyst.sap_po_open AS
SELECT analyst.num(s.raw->>'DocEntry')::bigint AS doc_entry,
       analyst.num(s.raw->>'DocNum')::bigint AS doc_num,
       analyst.num(s.raw->>'LineNum')::int AS line_num,
       analyst.day(s.raw->>'DocDate') AS doc_date,
       analyst.day(s.raw->>'DocDueDate') AS due_date,
       analyst.day(s.raw->>'ShipDate') AS ship_date,
       s.raw->>'CardCode' AS card_code,
       s.raw->>'CardName' AS card_name,
       analyst.name_key(s.raw->>'CardName') AS name_key,
       s.raw->>'ItemCode' AS item_code,
       s.raw->>'Dscription' AS item_name,
       s.raw->>'WhsCode' AS whs_code,
       analyst.num(s.raw->>'Quantity') AS quantity,
       analyst.num(s.raw->>'OpenQty') AS open_qty,
       analyst.num(s.raw->>'Price') AS price,
       s.raw->>'Currency' AS line_currency,
       analyst.num(s.raw->>'LineTotal') AS line_usd,
       s.raw->>'DocCur' AS doc_currency,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'po_open';

CREATE OR REPLACE VIEW analyst.sap_orders_open AS
SELECT analyst.num(s.raw->>'DocEntry')::bigint AS doc_entry,
       analyst.num(s.raw->>'DocNum')::bigint AS doc_num,
       analyst.day(s.raw->>'DocDate') AS doc_date,
       analyst.day(s.raw->>'DocDueDate') AS due_date,
       s.raw->>'CardCode' AS card_code,
       s.raw->>'CardName' AS card_name,
       analyst.name_key(s.raw->>'CardName') AS name_key,
       analyst.num(s.raw->>'SlpCode')::int AS slp_code,
       s.raw->>'DocCur' AS doc_currency,
       analyst.num(s.raw->>'DocTotal') AS total_usd,
       analyst.num(s.raw->>'DocTotalSy') AS total_som,
       analyst.num(s.raw->>'DocTotalFC') AS total_doc,
       s.raw->>'Comments' AS comments,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'orders';

CREATE OR REPLACE VIEW analyst.sap_service_calls AS
SELECT analyst.num(s.raw->>'callID')::bigint AS call_id,
       s.raw->>'subject' AS subject,
       s.raw->>'status' AS status,
       s.raw->>'priority' AS priority,
       s.raw->>'callType' AS call_type,
       s.raw->>'problemTyp' AS problem_type,
       s.raw->>'origin' AS origin,
       analyst.day(s.raw->>'createDate') AS created_on,
       analyst.day(s.raw->>'closeDate') AS closed_on,
       s.raw->>'customer' AS customer_code,
       s.raw->>'custmrName' AS customer_name,
       analyst.name_key(s.raw->>'custmrName') AS name_key,
       s.raw->>'itemCode' AS item_code,
       s.raw->>'itemName' AS item_name,
       s.raw->>'manufSN' AS serial,
       s.raw->>'internalSN' AS internal_serial,
       s.raw->>'TechnicianName' AS technician_name,
       s.raw->>'AssigneeName' AS assignee_name,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'service_calls';

CREATE OR REPLACE VIEW analyst.sap_equipment AS
SELECT analyst.num(s.raw->>'insID')::bigint AS ins_id,
       s.raw->>'customer' AS customer_code,
       s.raw->>'custmrName' AS customer_name,
       analyst.name_key(s.raw->>'custmrName') AS name_key,
       s.raw->>'itemCode' AS item_code,
       s.raw->>'itemName' AS item_name,
       s.raw->>'manufSN' AS serial,
       s.raw->>'internalSN' AS internal_serial,
       s.raw->>'status' AS status,
       analyst.day(s.raw->>'createDate') AS created_on,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'equipment';

CREATE OR REPLACE VIEW analyst.sap_service_contracts AS
SELECT analyst.num(s.raw->>'ContractID')::bigint AS contract_id,
       s.raw->>'CstmrCode' AS customer_code,
       s.raw->>'CstmrName' AS customer_name,
       analyst.name_key(s.raw->>'CstmrName') AS name_key,
       s.raw->>'CntrcType' AS contract_type,
       s.raw->>'Status' AS status,
       analyst.day(s.raw->>'StartDate') AS start_date,
       analyst.day(s.raw->>'EndDate') AS end_date,
       analyst.day(s.raw->>'TermDate') AS term_date,
       s.raw->>'Descriptio' AS description,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'service_contracts';

CREATE OR REPLACE VIEW analyst.sap_warehouses AS
SELECT s.raw->>'WhsCode' AS whs_code,
       s.raw->>'WhsName' AS whs_name,
       s.raw->>'Inactive' = 'Y' AS inactive,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'warehouses';

CREATE OR REPLACE VIEW analyst.sap_sales_people AS
SELECT analyst.num(s.raw->>'SlpCode')::int AS slp_code,
       s.raw->>'SlpName' AS slp_name,
       analyst.name_key(s.raw->>'SlpName') AS name_key,
       analyst.num(s.raw->>'GroupCode')::int AS group_code,
       coalesce(s.raw->>'Active', 'Y') = 'Y' AS active,
       s.captured_at AS captured_at
FROM v_sap_gateway_latest s WHERE s.tool = 'sales_people';

CREATE OR REPLACE VIEW analyst.ar_open AS
SELECT a.doc_entry AS doc_entry,
       a.doc_num AS doc_num,
       a.card_code AS card_code,
       a.card_name AS card_name,
       analyst.name_key(a.card_name) AS name_key,
       a.doc_date AS doc_date,
       a.due_date AS due_date,
       a.days_overdue AS days_overdue,
       a.aging_bucket AS aging_bucket,
       (a.balance_due_tiyin / 100.0)::numeric(18,2) AS balance_local,
       a.currency AS local_currency,
       CASE WHEN a.doc_currency IS NOT NULL AND a.doc_total_fc_tiyin - coalesce(a.paid_fc_tiyin, 0) > 0
            THEN ((a.doc_total_fc_tiyin - coalesce(a.paid_fc_tiyin, 0)) / 100.0)::numeric(18,2) END AS balance_as_written,
       CASE WHEN a.doc_currency IS NOT NULL AND a.doc_total_fc_tiyin - coalesce(a.paid_fc_tiyin, 0) > 0
            THEN a.doc_currency END AS written_currency,
       a.sales_person_name AS sales_person_name,
       a.division AS division,
       a.captured_at AS captured_at
FROM v_ar_aging_latest a;

CREATE OR REPLACE VIEW analyst.sales_month AS
SELECT f.day AS day,
       f.obj_type AS obj_type,
       f.doc_entry AS doc_entry,
       f.line_num AS line_num,
       f.slp_code AS slp_code,
       f.item_code AS item_code,
       f.item_group AS item_group,
       f.wh AS whs_code,
       f.amount_usd AS amount_usd,
       f.amount_som AS amount_som
FROM sales_facts f;

-- The bot's own records, Director only (no message texts: reports and proofs stay with their own tools).
CREATE OR REPLACE VIEW analyst.bot_tasks AS
SELECT t.code AS code,
       t.task_summary AS summary,
       t.status AS status,
       t.awaiting AS awaiting,
       t.due_date AS due_date,
       (t.created_at AT TIME ZONE 'Asia/Tashkent')::date AS created_on,
       (t.completed_at AT TIME ZONE 'Asia/Tashkent')::date AS completed_on,
       (t.verified_at AT TIME ZONE 'Asia/Tashkent')::date AS accepted_on,
       COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS assignee,
       e.role AS assignee_role,
       CASE WHEN t.source_message_id = 0 THEN COALESCE(t.code, t.id::text)  -- 0 = not a shared message (12.11)
            ELSE t.director_telegram_user_id::text || ':' || t.source_message_id::text END AS shared_key,
       t.status IN ('sent', 'started', 'blocked', 'review') AS is_open,
       t.priority AS priority,
       t.reschedules AS reschedules,
       t.proposed_due AS proposed_due,
       t.returns AS returns,
       COALESCE(NULLIF(btrim(acc.full_name), ''), acc.display_name) AS accepter,
       (t.expected_result IS NOT NULL) AS has_expected_result,
       CASE WHEN t.depends_on IS NOT NULL AND t.dependency_done_at IS NULL
                 AND t.status IN ('sent', 'started', 'blocked', 'review') THEN dep.code END AS waiting_for
FROM tasks t LEFT JOIN employees e ON e.id = t.assigned_employee_id
     LEFT JOIN employees acc ON acc.id = t.accepter_employee_id
     LEFT JOIN tasks dep ON dep.id = t.depends_on;

CREATE OR REPLACE VIEW analyst.bot_daily_reports AS
SELECT r.report_date AS report_date,
       COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS person,
       r.role AS role,
       r.status AS status,
       r.auto_built AS auto_built,
       (r.submitted_at AT TIME ZONE 'Asia/Tashkent') AS submitted_at,
       r.late_decision AS late_decision,
       coalesce(e.status = 'active', false) AS person_active
FROM daily_reports r LEFT JOIN employees e ON e.id = r.employee_id;

-- Trigram similarity for names (pg_trgm, a trusted extension) inside the analyst schema, so the analyst role finds it
-- on its search_path; never fatal — without it name search falls back to LIKE on name_key.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA analyst;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pg_trgm not installed: %', SQLERRM;
END $$;

-- The roles query_db runs as: SELECT on these views only — no public tables, no writes (also READ ONLY per query).
-- mgmg_analyst_sap: the SAP views (accounting with SAP access); mgmg_analyst_director: everything here. The app role
-- may SET ROLE to them but doesn't inherit them. Never fatal: without the roles query_db is simply not offered.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mgmg_analyst_sap') THEN
        CREATE ROLE mgmg_analyst_sap NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mgmg_analyst_director') THEN
        CREATE ROLE mgmg_analyst_director NOLOGIN;
    END IF;
    EXECUTE format('GRANT mgmg_analyst_sap TO %I WITH INHERIT FALSE, SET TRUE', current_user);
    EXECUTE format('GRANT mgmg_analyst_director TO %I WITH INHERIT FALSE, SET TRUE', current_user);
    GRANT USAGE ON SCHEMA analyst TO mgmg_analyst_sap, mgmg_analyst_director;
    GRANT SELECT ON analyst.sap_sales_docs, analyst.sap_sales_lines, analyst.sap_payments_in, analyst.sap_payments_out,
                    analyst.sap_ap_open, analyst.sap_supplier_balances, analyst.sap_customers, analyst.sap_products,
                    analyst.sap_stock, analyst.sap_stock_by_warehouse, analyst.sap_item_sales_90d, analyst.sap_po_open,
                    analyst.sap_orders_open, analyst.sap_service_calls, analyst.sap_equipment,
                    analyst.sap_service_contracts, analyst.sap_warehouses, analyst.sap_sales_people, analyst.ar_open,
                    analyst.sales_month
        TO mgmg_analyst_sap, mgmg_analyst_director;
    GRANT SELECT ON analyst.bot_tasks, analyst.bot_daily_reports TO mgmg_analyst_director;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'analyst roles not set up: %', SQLERRM;
END $$;

-- The scheduled jobs' daily claim (2026-11-11): the 08:00 / 16:00 / 17:00 jobs run from mgmg-api's own loop instead of
-- Render cron services (≈ $5/month). The job's own command claims (job, day) first; a second start the same day —
-- the old Render cron while it still exists, or a restart — finds it claimed and does nothing.
CREATE TABLE IF NOT EXISTS job_runs (
    job         TEXT         NOT NULL,
    day         DATE         NOT NULL,
    started_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (job, day)
);
-- A lease, not a one-shot claim (2026-11-11 review): a run beats every minute and is finished at the end; a run
-- killed by a deploy stops beating and the next start takes over (common/job_runs.py). Also «job:agent» rows: each
-- agent of the morning job starts once a day, so a resumed run never sends twice.
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
