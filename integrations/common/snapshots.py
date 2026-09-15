"""Persistence of daily snapshots into PostgreSQL.

Snapshots are append-only per day: re-running an agent on the same day updates
that day's rows in place (``ON CONFLICT DO UPDATE``) rather than duplicating
them, so a retry after a failure is safe.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from integrations.common.db import execute_many
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local
from integrations.crm.models import CRMStats, EmployeeReport, PipelineSummary
from integrations.sap.models import ARAging, CashAccount, SalesSummary

log = setup_logging("snapshots")


async def persist_ar_aging(aging: ARAging) -> int:
    """Write every open invoice of an AR aging run to ``ar_aging_snapshots``.

    Args:
        aging: The aging result from ``SAPClient.get_ar_aging``.

    Returns:
        Number of invoice rows written.

    Raises:
        psycopg.Error: on a database failure (the whole batch rolls back).
    """
    rows = [
        (
            aging.snapshot_date,
            inv.division,
            inv.doc_entry,
            inv.doc_num,
            inv.card_code,
            inv.card_name,
            inv.doc_date,
            inv.due_date,
            inv.days_overdue,
            inv.aging_bucket,
            inv.currency,
            inv.doc_total_tiyin,
            inv.paid_to_date_tiyin,
            inv.balance_due_tiyin,
            inv.sales_person_code,
            inv.sales_person_name,
        )
        for inv in aging.invoices
    ]

    written = await execute_many(
        """
        INSERT INTO ar_aging_snapshots
            (snapshot_date, division, doc_entry, doc_num, card_code, card_name,
             doc_date, due_date, days_overdue, aging_bucket, currency,
             doc_total_tiyin, paid_to_date_tiyin, balance_due_tiyin,
             sales_person_code, sales_person_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_date, doc_entry) DO UPDATE SET
            days_overdue       = EXCLUDED.days_overdue,
            aging_bucket       = EXCLUDED.aging_bucket,
            paid_to_date_tiyin = EXCLUDED.paid_to_date_tiyin,
            balance_due_tiyin  = EXCLUDED.balance_due_tiyin,
            captured_at        = now()
        """,
        rows,
    )
    log.info("Persisted {} AR row(s) for {}", written, aging.snapshot_date)
    return written


async def persist_cash_balances(accounts: Iterable[CashAccount], snapshot_date: date | None = None) -> int:
    """Write cash/bank balances to ``cash_balance_snapshots``.

    Args:
        accounts: Accounts from ``SAPClient.get_cash_balance``.
        snapshot_date: Business day; defaults to today in Tashkent.

    Returns:
        Number of rows written.

    Raises:
        psycopg.Error: on a database failure.
    """
    day = snapshot_date or today_local()
    rows = [
        (day, a.account_code, a.account_name, a.bank_name, a.currency, a.balance_tiyin)
        for a in accounts
    ]
    return await execute_many(
        """
        INSERT INTO cash_balance_snapshots
            (snapshot_date, account_code, account_name, bank_name, currency, balance_tiyin)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_date, account_code) DO UPDATE SET
            balance_tiyin = EXCLUDED.balance_tiyin,
            captured_at   = now()
        """,
        rows,
    )


# The CRM's own pipeline id — everything lands under one constant "pipeline"
# since there is only ever one sales pipeline in this CRM.
CRM_PIPELINE_ID = 1
CRM_PIPELINE_NAME = "MGMG CRM"


async def persist_crm_pipeline(summary: PipelineSummary, snapshot_date: date | None = None) -> int:
    """Write MGMG's own-CRM pipeline snapshot to ``amocrm_pipeline_snapshots``.

    The table name predates the move off amoCRM (one row per stage per day);
    it is kept rather than renamed so existing history and the
    ``v_pipeline_latest`` view keep working.

    Args:
        summary: The summary from ``CRMClient.get_pipeline_summary``.
        snapshot_date: Business day; defaults to today in Tashkent.

    Returns:
        Number of stage rows written.

    Raises:
        psycopg.Error: on a database failure.
    """
    day = snapshot_date or today_local()
    stalled_by_stage: dict[int | None, int] = {}
    for deal in summary.deals_without_task:
        stalled_by_stage[deal.stage_id] = stalled_by_stage.get(deal.stage_id, 0) + 1

    rows = [
        (
            day,
            CRM_PIPELINE_ID,
            CRM_PIPELINE_NAME,
            stage.stage_id,
            stage.name,
            None,  # this CRM has no division mapping yet
            stage.count,
            stage.value_tiyin,
            stalled_by_stage.get(stage.stage_id, 0),
            summary.new_leads_24h,
        )
        for stage in summary.stages
    ]
    return await execute_many(
        """
        INSERT INTO amocrm_pipeline_snapshots
            (snapshot_date, pipeline_id, pipeline_name, status_id, status_name,
             division, deals_count, deals_value_tiyin, deals_without_task, new_leads_24h)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_date, pipeline_id, status_id) DO UPDATE SET
            deals_count        = EXCLUDED.deals_count,
            deals_value_tiyin  = EXCLUDED.deals_value_tiyin,
            deals_without_task = EXCLUDED.deals_without_task,
            new_leads_24h      = EXCLUDED.new_leads_24h,
            captured_at        = now()
        """,
        rows,
    )


async def persist_crm_stats(stats: CRMStats, snapshot_date: date | None = None) -> int:
    """Write the whole-CRM aggregate (contacts, conversion) to ``crm_stats_snapshots``.

    Args:
        stats: The summary from ``CRMClient.get_stats``.
        snapshot_date: Business day; defaults to today in Tashkent.

    Returns:
        1 (one row written).

    Raises:
        psycopg.Error: on a database failure.
    """
    day = snapshot_date or today_local()
    return await execute_many(
        """
        INSERT INTO crm_stats_snapshots
            (snapshot_date, total_deals, total_value_tiyin, total_contacts,
             won_deals, won_value_tiyin, conversion_rate)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_date) DO UPDATE SET
            total_deals       = EXCLUDED.total_deals,
            total_value_tiyin = EXCLUDED.total_value_tiyin,
            total_contacts    = EXCLUDED.total_contacts,
            won_deals         = EXCLUDED.won_deals,
            won_value_tiyin   = EXCLUDED.won_value_tiyin,
            conversion_rate   = EXCLUDED.conversion_rate,
            captured_at       = now()
        """,
        [(
            day,
            stats.total_deals,
            stats.total_value_tiyin,
            stats.total_contacts,
            stats.won_deals,
            stats.won_value_tiyin,
            stats.conversion_rate,
        )],
    )


async def sync_crm_reports(reports: list[EmployeeReport]) -> int:
    """Upsert employee reports into ``crm_employee_reports`` by the CRM's own id.

    Not a daily snapshot like the functions above — each report already has a
    stable id and submitted_at from the CRM, so this is a plain sync rather
    than something tied to "today".

    Args:
        reports: Reports from ``CRMClient.get_reports``.

    Returns:
        Number of rows written (0 if ``reports`` is empty).

    Raises:
        psycopg.Error: on a database failure.
    """
    if not reports:
        return 0
    return await execute_many(
        """
        INSERT INTO crm_employee_reports
            (id, manager_id, manager_name, report_type, report_date, content, submitted_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            manager_id    = EXCLUDED.manager_id,
            manager_name  = EXCLUDED.manager_name,
            report_type   = EXCLUDED.report_type,
            report_date   = EXCLUDED.report_date,
            content       = EXCLUDED.content,
            submitted_at  = EXCLUDED.submitted_at,
            synced_at     = now()
        """,
        [
            (
                r.id,
                r.manager_id,
                r.manager_name,
                r.report_type,
                r.report_date.date() if r.report_date else None,
                r.content,
                r.submitted_at,
            )
            for r in reports
        ],
    )


async def persist_sales_summary(summary: SalesSummary, snapshot_date: date | None = None) -> int:
    """Write a per-division sales summary to ``sales_summary_snapshots``.

    Args:
        summary: The summary from ``SAPClient.get_sales_summary``.
        snapshot_date: Business day; defaults to today in Tashkent.

    Returns:
        Number of division rows written.

    Raises:
        psycopg.Error: on a database failure.
    """
    day = snapshot_date or today_local()
    rows = [
        (day, summary.period_start, summary.period_end, division, 0, amount, summary.currency)
        for division, amount in summary.by_division.items()
    ]
    return await execute_many(
        """
        INSERT INTO sales_summary_snapshots
            (snapshot_date, period_start, period_end, division,
             invoices_count, gross_total_tiyin, currency)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_date, period_start, period_end, division) DO UPDATE SET
            gross_total_tiyin = EXCLUDED.gross_total_tiyin,
            captured_at       = now()
        """,
        rows,
    )
