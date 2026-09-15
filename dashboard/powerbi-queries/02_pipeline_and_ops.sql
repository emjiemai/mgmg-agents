-- ============================================================================
-- Power BI source queries: CRM pipeline, operations and agent health
-- ============================================================================

-- ---------------------------------------------------------------------------
-- QUERY: Pipeline
-- One row per pipeline stage per day. The table name predates the move off
-- amoCRM; MGMG's own CRM writes here now.
-- ---------------------------------------------------------------------------
SELECT
    snapshot_date                                AS "Date",
    pipeline_id                                  AS "Pipeline Id",
    COALESCE(pipeline_name, pipeline_id::text)   AS "Pipeline",
    status_id                                    AS "Stage Id",
    COALESCE(status_name, status_id::text)       AS "Stage",
    COALESCE(division, 'unmapped')               AS "Division",
    deals_count                                  AS "Deals",
    (deals_value_tiyin / 100.0)::numeric(18,2)   AS "Pipeline Value UZS",
    deals_without_task                           AS "Deals Without Task",
    new_leads_24h                                AS "New Leads 24h"
FROM amocrm_pipeline_snapshots
WHERE snapshot_date >= current_date - INTERVAL '18 months'
ORDER BY snapshot_date DESC, deals_value_tiyin DESC;

-- ---------------------------------------------------------------------------
-- QUERY: Daily Briefs
-- The headline numbers of every brief sent — the CEO's own trend line.
-- ---------------------------------------------------------------------------
SELECT
    brief_date                                       AS "Date",
    status                                           AS "Status",
    generated_at AT TIME ZONE 'Asia/Tashkent'        AS "Generated (Tashkent)",
    sent_at AT TIME ZONE 'Asia/Tashkent'             AS "Sent (Tashkent)",
    (cash_total_tiyin / 100.0)::numeric(18,2)        AS "Cash UZS",
    (ar_overdue_total_tiyin / 100.0)::numeric(18,2)  AS "Overdue AR UZS",
    (pipeline_total_tiyin / 100.0)::numeric(18,2)    AS "Pipeline UZS",
    new_leads_24h                                    AS "New Leads",
    deals_without_task                               AS "Stalled Deals",
    jsonb_array_length(source_errors)                AS "Failed Sources"
FROM daily_briefs
ORDER BY brief_date DESC;

-- ---------------------------------------------------------------------------
-- QUERY: Agent Health
-- Daily success/failure counts per agent and target system. Put this on a
-- hidden admin page — if an agent starts failing, the CEO's numbers go stale
-- and this is the only place that shows it.
-- ---------------------------------------------------------------------------
SELECT
    (occurred_at AT TIME ZONE 'Asia/Tashkent')::date AS "Date",
    agent                                            AS "Agent",
    target_system                                    AS "System",
    mode                                             AS "Mode",
    status                                           AS "Status",
    count(*)                                         AS "Calls",
    round(avg(duration_ms))                          AS "Avg ms",
    max(duration_ms)                                 AS "Max ms"
FROM agent_actions
WHERE occurred_at >= now() - INTERVAL '90 days'
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1 DESC, "Calls" DESC;

-- ---------------------------------------------------------------------------
-- QUERY: Alerts
-- Alert history with acknowledgement state.
-- ---------------------------------------------------------------------------
SELECT
    (created_at AT TIME ZONE 'Asia/Tashkent')::date  AS "Date",
    agent                                            AS "Agent",
    severity                                         AS "Severity",
    COALESCE(division, 'unmapped')                   AS "Division",
    title                                            AS "Alert",
    COALESCE(owner, 'Unassigned')                    AS "Owner",
    status                                           AS "Status",
    (amount_tiyin / 100.0)::numeric(18,2)            AS "Amount UZS",
    acknowledged_at AT TIME ZONE 'Asia/Tashkent'     AS "Acknowledged (Tashkent)"
FROM alerts
WHERE created_at >= now() - INTERVAL '12 months'
ORDER BY created_at DESC;

-- ---------------------------------------------------------------------------
-- QUERY: Date
-- Date dimension driving every relationship in the model. Power BI's automatic
-- date tables are disabled for this report — one shared Date table keeps
-- cross-source filtering (cash vs AR vs pipeline) consistent.
-- ---------------------------------------------------------------------------
SELECT
    d::date                                    AS "Date",
    EXTRACT(YEAR FROM d)::int                  AS "Year",
    EXTRACT(QUARTER FROM d)::int               AS "Quarter",
    EXTRACT(MONTH FROM d)::int                 AS "Month Number",
    to_char(d, 'Mon')                          AS "Month",
    to_char(d, 'YYYY-MM')                      AS "Year Month",
    EXTRACT(DAY FROM d)::int                   AS "Day",
    EXTRACT(ISODOW FROM d)::int                AS "Weekday Number",
    to_char(d, 'Dy')                           AS "Weekday",
    (EXTRACT(ISODOW FROM d) >= 6)              AS "Is Weekend"
FROM generate_series(
        date_trunc('year', current_date - INTERVAL '2 years'),
        date_trunc('year', current_date) + INTERVAL '1 year' - INTERVAL '1 day',
        INTERVAL '1 day'
     ) AS d;
