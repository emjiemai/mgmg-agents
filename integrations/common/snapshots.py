"""Persistence of daily snapshots into PostgreSQL.

Snapshots are append-only per day: re-running an agent on the same day updates
that day's rows in place (``ON CONFLICT DO UPDATE``) rather than duplicating
them, so a retry after a failure is safe.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from integrations.amocrm.models import PipelineSummary
from integrations.common.db import execute_many
from integrations.common.divisions import division_from_amocrm
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local
from integrations.crm.models import PipelineSummary as CRMPipelineSummary
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


async def persist_pipeline(summary: PipelineSummary, snapshot_date: date | None = None) -> int:
    """Write an amoCRM pipeline snapshot to ``amocrm_pipeline_snapshots``.

    Args:
        summary: The summary from ``AmoCRMClient.get_pipeline_summary``.
        snapshot_date: Business day; defaults to today in Tashkent.

    Returns:
        Number of stage rows written.

    Raises:
        psycopg.Error: on a database failure.
    """
    day = snapshot_date or today_local()
    rows = [
        (
            day,
            stage.pipeline_id,
            stage.pipeline_name,
            stage.status_id,
            stage.status_name,
            stage.division or division_from_amocrm(stage.pipeline_id),
            stage.deals_count,
            stage.deals_value_tiyin,
            stage.deals_without_task,
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


# The CRM's own pipeline id — everything lands under one constant "pipeline"
# since (unlike amoCRM) there is only ever one sales pipeline in this CRM.
CRM_PIPELINE_ID = 1
CRM_PIPELINE_NAME = "MGMG CRM"


async def persist_crm_pipeline(summary: CRMPipelineSummary, snapshot_date: date | None = None) -> int:
    """Write MGMG's own-CRM pipeline snapshot to ``amocrm_pipeline_snapshots``.

    Reuses the same table as the old amoCRM snapshots (same reporting shape:
    one row per stage per day) rather than adding a parallel table, since the
    CRM this replaces and the one before it both describe "deals by stage."

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


async def persist_planner_tasks(tasks: list[dict[str, Any]], snapshot_date: date | None = None) -> int:
    """Write overdue Planner tasks to ``planner_task_snapshots``.

    Args:
        tasks: Task dicts from ``GraphClient.get_overdue_planner_tasks``.
        snapshot_date: Business day; defaults to today in Tashkent.

    Returns:
        Number of rows written.

    Raises:
        psycopg.Error: on a database failure.
    """
    day = snapshot_date or today_local()
    rows = [
        (
            day,
            task.get("id"),
            task.get("planId"),
            task.get("bucketId"),
            task.get("title"),
            ", ".join(task.get("assignedToNames", [])) or None,
            task.get("dueAt"),
            task.get("percentComplete"),
            True,
            task.get("daysOverdue"),
        )
        for task in tasks
    ]
    return await execute_many(
        """
        INSERT INTO planner_task_snapshots
            (snapshot_date, task_id, plan_id, bucket_id, title, assigned_to,
             due_at, percent_complete, is_overdue, days_overdue)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_date, task_id) DO UPDATE SET
            days_overdue     = EXCLUDED.days_overdue,
            percent_complete = EXCLUDED.percent_complete,
            captured_at      = now()
        """,
        rows,
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
