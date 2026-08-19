-- ============================================================================
-- Power BI source query: Cash position + AR aging (SAP)
-- Connection: PostgreSQL, database "mgmg", Import mode (not DirectQuery —
--   snapshots change once a day, so a scheduled refresh is cheaper and faster).
-- Load as two separate queries; do not join them in SQL, let the model relate
--   them through the Date table.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- QUERY: Cash
-- One row per bank/cash account per day. Money exposed in UZS (not tiyin) so
-- Power BI never has to know about the storage unit.
-- ---------------------------------------------------------------------------
SELECT
    snapshot_date                              AS "Date",
    account_code                               AS "Account Code",
    COALESCE(bank_name, account_name, account_code) AS "Account",
    currency                                   AS "Currency",
    (balance_tiyin / 100.0)::numeric(18,2)     AS "Balance UZS"
FROM cash_balance_snapshots
WHERE snapshot_date >= current_date - INTERVAL '18 months'
ORDER BY snapshot_date DESC, account_code;

-- ---------------------------------------------------------------------------
-- QUERY: AR Aging
-- One row per open invoice per day. Includes not-yet-due invoices so the model
-- can show total open AR alongside the overdue slice.
-- ---------------------------------------------------------------------------
SELECT
    a.snapshot_date                            AS "Date",
    a.doc_entry                                AS "Doc Entry",
    a.doc_num                                  AS "Invoice No",
    a.card_code                                AS "Customer Code",
    a.card_name                                AS "Customer",
    COALESCE(a.division, 'unmapped')           AS "Division",
    COALESCE(a.sales_person_name, 'Unassigned') AS "Owner",
    a.doc_date                                 AS "Invoice Date",
    a.due_date                                 AS "Due Date",
    a.days_overdue                             AS "Days Overdue",
    a.aging_bucket                             AS "Bucket Key",
    CASE a.aging_bucket
        WHEN 'current' THEN 'Not due'
        WHEN '1_30'    THEN '1-30 days'
        WHEN '31_60'   THEN '31-60 days'
        WHEN '61_90'   THEN '61-90 days'
        WHEN '90_plus' THEN '90+ days'
    END                                        AS "Bucket",
    CASE a.aging_bucket
        WHEN 'current' THEN 0
        WHEN '1_30'    THEN 1
        WHEN '31_60'   THEN 2
        WHEN '61_90'   THEN 3
        WHEN '90_plus' THEN 4
    END                                        AS "Bucket Sort",
    a.currency                                 AS "Currency",
    (a.balance_due_tiyin / 100.0)::numeric(18,2) AS "Balance Due UZS",
    (a.doc_total_tiyin  / 100.0)::numeric(18,2)  AS "Invoice Total UZS",
    (a.days_overdue > 0)                       AS "Is Overdue"
FROM ar_aging_snapshots a
WHERE a.snapshot_date >= current_date - INTERVAL '18 months'
ORDER BY a.snapshot_date DESC, a.balance_due_tiyin DESC;

-- ---------------------------------------------------------------------------
-- QUERY: Sales
-- Month-to-date sales by division, as captured each day.
-- ---------------------------------------------------------------------------
SELECT
    snapshot_date                              AS "Date",
    period_start                               AS "Period Start",
    period_end                                 AS "Period End",
    COALESCE(division, 'unmapped')             AS "Division",
    invoices_count                             AS "Invoices",
    (gross_total_tiyin / 100.0)::numeric(18,2) AS "Sales UZS",
    currency                                   AS "Currency"
FROM sales_summary_snapshots
WHERE snapshot_date >= current_date - INTERVAL '18 months'
ORDER BY snapshot_date DESC, division;
