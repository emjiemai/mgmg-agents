"""Agent 2 — amoCRM Follow-up Agent.

Trigger: an amoCRM webhook fires on a deal event (new lead, stage change,
update). The agent checks whether the deal has a next task scheduled. If it
does not, the deal is stalling, so the agent:

    1. creates a Planner task in the Command Center plan (single source of
       truth for company tasks),
    2. creates a matching amoCRM task on the deal so the manager sees it in the
       tool they actually work in,
    3. pings the responsible manager in the Teams sales channel.

All three writes respect ``AGENT_WRITES_ENABLED`` (security rule #1): while the
gate is closed, every intended write is audited as a dry run and nothing leaves
the VPS. Turning the gate on is the only change needed to go live.

Run modes:
    python agents/amocrm-followup/agent.py --lead 12345   # one deal, on demand
    python agents/amocrm-followup/agent.py --sweep        # every stalled deal
    python agents/amocrm-followup/agent.py --drain        # pending webhook events

The webhook path (``integrations/amocrm/webhook_handler.py``) imports
``process_lead`` from this module.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.amocrm.client import AmoCRMClient
from integrations.amocrm.models import Lead
from integrations.common.config import settings
from integrations.common.db import close_pool, execute, fetch_all, log_action
from integrations.common.divisions import label as division_label
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_uzs_short
from integrations.common.timeutil import fmt_local, now_utc
from integrations.microsoft.client import GraphClient, GraphError
from integrations.microsoft.teams_notify import notify_channel

AGENT = "amocrm-followup"
log = setup_logging(AGENT)

# How long the manager gets before the follow-up task is due.
FOLLOWUP_DUE_HOURS = 24

# Deals in these amoCRM stages are finished and never need a follow-up task.
CLOSED_STATUS_IDS = {142, 143}  # 142 = won, 143 = lost


async def process_lead(lead_id: int, run_id: uuid.UUID | None = None) -> dict[str, object]:
    """Ensure one deal has a next action, creating it if it does not.

    Args:
        lead_id: The amoCRM deal id.
        run_id: UUID grouping this run's audit rows; generated if omitted.

    Returns:
        A result dict: ``{"lead_id", "action", "planner_task_id",
        "amocrm_task_id", "teams_notified"}`` where ``action`` is one of
        'created', 'skipped_has_task', 'skipped_closed', 'skipped_not_found'.

    Raises:
        AmoCRMError: if amoCRM is unreachable.
    """
    run_id = run_id or uuid.uuid4()
    result: dict[str, object] = {
        "lead_id": lead_id,
        "action": "skipped_not_found",
        "planner_task_id": None,
        "amocrm_task_id": None,
        "teams_notified": False,
    }

    async with AmoCRMClient(agent=AGENT, run_id=run_id) as amo:
        lead = await amo.get_lead(lead_id)

        if lead is None:
            log.info("Lead {} not found (deleted?) — nothing to do", lead_id)
            return result

        if lead.status_id in CLOSED_STATUS_IDS or lead.is_closed:
            result["action"] = "skipped_closed"
            log.debug("Lead {} is closed — no follow-up needed", lead_id)
            return result

        if lead.has_next_task:
            result["action"] = "skipped_has_task"
            log.debug(
                "Lead {} already has a task due {} — no follow-up needed",
                lead_id,
                fmt_local(lead.closest_task_at),
            )
            return result

        due = now_utc() + timedelta(hours=FOLLOWUP_DUE_HOURS)
        task_text = _task_text(lead)

        result["amocrm_task_id"] = await amo.create_task(
            lead_id=lead.id,
            text=task_text,
            due_date=due,
            responsible_id=lead.responsible_user_id,
        )

    result["planner_task_id"] = await _create_planner_task(lead, task_text, due, run_id)
    result["teams_notified"] = await _ping_manager(lead, run_id)
    result["action"] = "created"

    await log_action(
        agent=AGENT,
        action="followup_created",
        target_system="amocrm",
        status="dry_run" if not settings.agent_writes_enabled else "success",
        run_id=run_id,
        target_ref=f"lead:{lead.id}",
        mode="write",
        payload={
            "lead_name": lead.name,
            "value_tiyin": lead.price_tiyin,
            "responsible": lead.responsible_user_name,
            "planner_task_id": result["planner_task_id"],
            "amocrm_task_id": result["amocrm_task_id"],
        },
    )

    log.info(
        "Follow-up created for lead {} ({}), owner {}",
        lead.id,
        lead.name,
        lead.responsible_user_name or "unassigned",
    )
    return result


async def _create_planner_task(lead: Lead, text: str, due, run_id: uuid.UUID) -> str | None:
    """Mirror the follow-up into Microsoft Planner.

    Args:
        lead: The stalled deal.
        text: Task title.
        due: Due datetime.
        run_id: UUID grouping this run's audit rows.

    Returns:
        The Planner task id, or ``None`` when writes are gated or Planner is
        unavailable. Planner problems never fail the run — the amoCRM task and
        the Teams ping still stand on their own.
    """
    plan_id = settings.ms_planner_default_plan_id
    if not plan_id:
        log.warning("MS_PLANNER_DEFAULT_PLAN_ID is not set — skipping the Planner mirror")
        return None

    try:
        async with GraphClient(agent=AGENT, run_id=run_id) as graph:
            return await graph.create_planner_task(plan_id=plan_id, title=text, due_date=due)
    except GraphError as err:
        log.error("Planner task creation failed for lead {}: {}", lead.id, err)
        return None


async def _ping_manager(lead: Lead, run_id: uuid.UUID) -> bool:
    """Notify the responsible manager in the Teams sales channel.

    Args:
        lead: The stalled deal.
        run_id: UUID grouping this run's audit rows.

    Returns:
        True if Teams accepted the message.
    """
    owner = lead.responsible_user_name or "Unassigned"
    return await notify_channel(
        title=f"⏳ Deal with no next step: {lead.name or lead.id}",
        text=(
            f"**{owner}**, this deal has no scheduled task. "
            f"A follow-up task due in {FOLLOWUP_DUE_HOURS}h has been created."
        ),
        facts={
            "Deal": lead.name or str(lead.id),
            "Value": format_uzs_short(lead.price_tiyin),
            "Stage": lead.status_name or str(lead.status_id),
            "Division": division_label(lead.division),
            "Owner": owner,
        },
        link_url=f"{settings.amocrm_base_url}{lead.url}",
        link_label="Open in amoCRM",
        agent=AGENT,
        run_id=run_id,
    )


def _task_text(lead: Lead) -> str:
    """Compose the follow-up task title.

    Args:
        lead: The stalled deal.

    Returns:
        A title naming the deal and its value, short enough for both Planner
        and amoCRM.
    """
    name = lead.name or f"Deal {lead.id}"
    return f"Follow up: {name} ({format_uzs_short(lead.price_tiyin)})"[:255]


# ------------------------------------------------------------------ batch runs


async def sweep(run_id: uuid.UUID | None = None) -> list[dict[str, object]]:
    """Scan the whole pipeline and create follow-ups for every stalled deal.

    This is the safety net for webhooks that were missed while the VPS was
    down. Run it nightly.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        One result dict per deal processed.

    Raises:
        AmoCRMError: if amoCRM is unreachable.
    """
    run_id = run_id or uuid.uuid4()

    async with AmoCRMClient(agent=AGENT, run_id=run_id) as amo:
        summary = await amo.get_pipeline_summary()

    stalled = summary.deals_without_task
    log.info("Sweep: {} stalled deal(s) of {}", len(stalled), summary.total_deals)

    results = []
    for lead in stalled:
        results.append(await process_lead(lead.id, run_id))
    return results


async def drain_pending_events(limit: int = 100) -> int:
    """Process webhook events that were stored but not yet acted on.

    The webhook endpoint always persists first and processes second, so a
    crash between the two leaves rows in ``pending`` — this drains them.

    Args:
        limit: Maximum events to process in one pass.

    Returns:
        Number of events processed.

    Raises:
        psycopg.Error: on a database failure.
    """
    run_id = uuid.uuid4()
    events = await fetch_all(
        """
        SELECT id, lead_id FROM amocrm_deal_events
        WHERE processing_status = 'pending' AND lead_id IS NOT NULL
        ORDER BY received_at
        LIMIT %s
        """,
        (limit,),
    )

    for event in events:
        try:
            result = await process_lead(int(event["lead_id"]), run_id)
        except Exception as exc:  # noqa: BLE001 — one bad event must not stop the drain
            log.error("Event {} failed: {}", event["id"], exc)
            await execute(
                """
                UPDATE amocrm_deal_events
                SET processing_status = 'failed', processed_at = now(), processing_error = %s
                WHERE id = %s
                """,
                (str(exc)[:1000], event["id"]),
            )
            continue

        status = "processed" if result["action"] == "created" else "ignored"
        await execute(
            "UPDATE amocrm_deal_events SET processing_status = %s, processed_at = now() WHERE id = %s",
            (status, event["id"]),
        )

    log.info("Drained {} pending event(s)", len(events))
    return len(events)


# ------------------------------------------------------------------------ main


async def _main(args: argparse.Namespace) -> int:
    """Dispatch the selected mode and close the database pool.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    try:
        if args.lead:
            result = await process_lead(args.lead)
            print(result)
        elif args.sweep:
            results = await sweep()
            print(f"{sum(1 for r in results if r['action'] == 'created')} follow-up(s) created")
        elif args.drain:
            count = await drain_pending_events()
            print(f"{count} event(s) drained")
        else:
            print("Nothing to do — pass --lead, --sweep or --drain")
            return 2
        return 0
    finally:
        await close_pool()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="amoCRM follow-up agent.")
    parser.add_argument("--lead", type=int, help="process a single deal by id")
    parser.add_argument("--sweep", action="store_true", help="process every stalled deal")
    parser.add_argument("--drain", action="store_true", help="process pending webhook events")
    parser.add_argument("--dry-run", action="store_true", help="compute but never write")
    args = parser.parse_args()

    if args.dry_run:
        settings.dry_run = True

    try:
        sys.exit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
