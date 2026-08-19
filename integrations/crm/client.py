"""Client for MGMG's own sales CRM (sales-crm-roan-six.vercel.app).

Read-only by construction, not just by convention: the API key Abdulbosit
issued has no write scope at all — there is no POST/PUT/DELETE route under
``/api/external/*``, confirmed by the CRM's own team ("all read-only, GET
only, no way to write or delete through this key"). That means this client
has no ``create_*`` methods, unlike the SAP/amoCRM/Graph clients — there is
nothing to gate behind ``AGENT_WRITES_ENABLED`` here because there is nothing
to write.

No Vercel deployment-protection bypass header is needed — confirmed live on
2026-08-18, the API key alone reaches the app's own route handler.

Auth: a static ``X-API-Key`` header, not a session or OAuth token, so there is
no login/refresh step like the SAP or Graph clients have.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import httpx

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.common.money import to_tiyin
from integrations.common.timeutil import UTC, last_24h_utc
from integrations.crm.models import (
    CRMStats,
    Contact,
    Deal,
    ManagerTask,
    MonthlyBreakdown,
    PipelineSummary,
    StageBreakdown,
)

log = setup_logging("crm")

# stage_id 4 = "Deal/Paid project" (won), stage_id 6 = "Fail" (lost) — read
# from the live /stats response on 2026-08-18. Re-check if stages are ever
# renumbered in the CRM.
TERMINAL_STAGE_IDS = {4, 6}
WON_STAGE_ID = 4

# Candidate field names for the task->deal link. /manager-tasks returned an
# empty list at integration time, so this is unconfirmed — _extract_deal_id
# logs a warning naming the actual keys seen if none of these match, so a
# wrong guess here is diagnosable rather than silently wrong.
DEAL_ID_CANDIDATES = ("dealId", "deal_id", "relatedDealId", "leadId")


class CRMError(RuntimeError):
    """Raised when the CRM API returns an error the client cannot recover from."""


class CRMClient:
    """Async client for MGMG's internal sales CRM.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None
        self._stage_names: dict[int, str] | None = None

    async def __aenter__(self) -> "CRMClient":
        """Open the HTTP client with the API key attached.

        Returns:
            The ready client.

        Raises:
            CRMError: if the base URL or API key is unset.
        """
        if not settings.crm_base_url or not settings.crm_api_key.get_secret_value():
            raise CRMError("CRM is not configured — fill CRM_BASE_URL / CRM_API_KEY in .env")

        self._client = httpx.AsyncClient(
            base_url=settings.crm_base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0),
            headers={"X-API-Key": settings.crm_api_key.get_secret_value()},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- requests

    async def _get(self, path: str) -> Any:
        """Issue one audited GET against the CRM API.

        Args:
            path: Path below ``/api/external``, e.g. ``/deals``.

        Returns:
            The parsed JSON body (list or dict, depending on the endpoint).

        Raises:
            CRMError: on a non-200 response.
        """
        assert self._client is not None
        full_path = f"/api/external{path}"

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system="crm",
            run_id=self.run_id,
            target_ref=full_path,
            mode="read",
        ) as ctx:
            response = await request_with_retry(self._client, "GET", full_path)
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise CRMError(f"CRM GET {full_path} failed: HTTP {response.status_code} {response.text[:300]}")
            body = response.json()
            ctx["payload"]["rows"] = len(body) if isinstance(body, list) else 1
            return body

    # ----------------------------------------------------------------- queries

    async def get_stats(self) -> CRMStats:
        """Fetch the whole-CRM summary from /stats.

        Returns:
            A ``CRMStats`` with money in tiyin.

        Raises:
            CRMError: on an API error.
        """
        body = await self._get("/stats")
        stats = CRMStats(
            total_deals=body.get("totalDeals", 0),
            total_value_tiyin=to_tiyin(body.get("totalValue")),
            total_contacts=body.get("totalContacts", 0),
            won_deals=body.get("wonDeals", 0),
            won_value_tiyin=to_tiyin(body.get("wonValue")),
            conversion_rate=float(body.get("conversionRate") or 0),
            by_stage=[
                StageBreakdown(
                    stage_id=s["stageId"],
                    name=s.get("name"),
                    color=s.get("color"),
                    count=s.get("count", 0),
                    value_tiyin=to_tiyin(s.get("value")),
                )
                for s in body.get("byStage", [])
            ],
            monthly=[
                MonthlyBreakdown(label=m["label"], count=m.get("count", 0), value_tiyin=to_tiyin(m.get("value")))
                for m in body.get("monthly", [])
            ],
        )
        log.info("CRM stats: {} deal(s), {} contact(s)", stats.total_deals, stats.total_contacts)
        return stats

    async def get_deals(self) -> list[Deal]:
        """Fetch every deal, with stage names filled in from /stats.

        Returns:
            All deals, most recently created first (as the API returns them).

        Raises:
            CRMError: on an API error.
        """
        rows = await self._get("/deals")
        stage_names = await self._get_stage_names()

        deals = [
            Deal(
                id=row["id"],
                title=row.get("title"),
                contact_id=row.get("contactId"),
                amount_tiyin=to_tiyin(row.get("amount")),
                currency=row.get("currency") or "UZS",
                stage_id=row.get("stageId"),
                stage_name=stage_names.get(row.get("stageId")),
                assigned_to=row.get("assignedTo"),
                created_by=row.get("createdBy"),
                created_at=_ts(row.get("createdAt")),
                updated_at=_ts(row.get("updatedAt")),
            )
            for row in rows
        ]
        log.info("CRM: {} deal(s) fetched", len(deals))
        return deals

    async def get_contacts(self) -> list[Contact]:
        """Fetch every contact.

        Returns:
            All contacts.

        Raises:
            CRMError: on an API error.
        """
        rows = await self._get("/contacts")
        contacts = [
            Contact(
                id=row["id"],
                name=row.get("name"),
                company=row.get("company"),
                phone=row.get("phone"),
                email=row.get("email") or None,
                note=row.get("note") or None,
                created_by=row.get("createdBy"),
                created_by_name=row.get("createdByName"),
                created_at=_ts(row.get("createdAt")),
                updated_at=_ts(row.get("updatedAt")),
            )
            for row in rows
        ]
        log.info("CRM: {} contact(s) fetched", len(contacts))
        return contacts

    async def get_manager_tasks(self) -> list[ManagerTask]:
        """Fetch every open manager task.

        The task->deal link field is unconfirmed (see module docstring) — a
        task whose deal cannot be identified logs a warning naming its actual
        keys and is treated as unlinked (``deal_id=None``) rather than
        guessed at silently.

        Returns:
            All tasks returned by the API.

        Raises:
            CRMError: on an API error.
        """
        rows = await self._get("/manager-tasks")
        tasks = [
            ManagerTask(
                id=row["id"],
                text=row.get("text") or row.get("title") or row.get("description"),
                deal_id=_extract_deal_id(row),
                assigned_to=row.get("assignedTo") or row.get("managerId"),
                due_at=_ts(row.get("dueAt") or row.get("dueDate") or row.get("completeTill")),
                is_completed=bool(row.get("isCompleted") or row.get("completed") or row.get("done")),
            )
            for row in rows
        ]
        log.info("CRM: {} manager task(s) fetched", len(tasks))
        return tasks

    async def get_pipeline_summary(self) -> PipelineSummary:
        """Build a CEO-brief-ready pipeline summary from deals + tasks + stats.

        Returns:
            A ``PipelineSummary`` shaped like the old amoCRM one, so the CEO
            brief's rendering code needs no changes.

        Raises:
            CRMError: on an API error.
        """
        deals = await self.get_deals()
        tasks = await self.get_manager_tasks()
        stats = await self.get_stats()
        window_start, _ = last_24h_utc()

        open_task_deal_ids = {t.deal_id for t in tasks if t.deal_id is not None and not t.is_completed}
        for deal in deals:
            deal.has_next_task = deal.id in open_task_deal_ids

        open_deals = [d for d in deals if not d.is_closed]
        without_task = sorted(
            (d for d in open_deals if not d.has_next_task), key=lambda d: d.amount_tiyin, reverse=True
        )
        new_leads_24h = sum(1 for d in deals if d.created_at and d.created_at >= window_start)

        summary = PipelineSummary(
            stages=stats.by_stage,
            total_deals=len(open_deals),
            total_value_tiyin=sum(d.amount_tiyin for d in open_deals),
            deals_without_task=without_task,
            new_leads_24h=new_leads_24h,
        )
        log.info(
            "CRM pipeline: {} open deal(s), {} without a next task, {} new in 24h",
            summary.total_deals,
            len(summary.deals_without_task),
            summary.new_leads_24h,
        )
        return summary

    # ----------------------------------------------------------------- helpers

    async def _get_stage_names(self) -> dict[int, str]:
        """Fetch and cache stage id -> name from /stats.

        Returns:
            Mapping of stage id to name; empty if /stats fails (deals still
            work, just without stage names attached).
        """
        if self._stage_names is not None:
            return self._stage_names
        try:
            stats = await self.get_stats()
        except CRMError as err:
            log.warning("Could not load CRM stage names: {}", err)
            self._stage_names = {}
            return self._stage_names
        self._stage_names = {s.stage_id: s.name or str(s.stage_id) for s in stats.by_stage}
        return self._stage_names


def _extract_deal_id(row: dict[str, Any]) -> int | None:
    """Try each candidate key for the task->deal link field.

    Args:
        row: One raw manager-task record.

    Returns:
        The linked deal id, or ``None`` if no candidate key matched — logged
        once per call so a wrong guess is visible in production logs instead
        of silently producing false "no next task" alerts.
    """
    for key in DEAL_ID_CANDIDATES:
        if row.get(key) is not None:
            return int(row[key])
    log.warning(
        "manager-task {} has none of {} — deal linkage unknown, keys seen: {}",
        row.get("id"),
        DEAL_ID_CANDIDATES,
        sorted(row.keys()),
    )
    return None


def _ts(value: str | None) -> datetime | None:
    """Parse the CRM's ISO-8601 timestamp strings.

    Args:
        value: e.g. ``"2026-08-18T11:44:30.900Z"``.

    Returns:
        Aware UTC datetime, or ``None`` if unparseable.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
