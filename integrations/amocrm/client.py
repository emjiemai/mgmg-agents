"""amoCRM / Kommo API v4 client.

Auth is a long-lived access token (OAuth2 bearer) issued from the integration
card in amoCRM — no refresh dance, no client secret at runtime.

Rate limiting: amoCRM allows 7 requests/second per account and blocks the
integration on sustained breach, so every request passes through a shared
``RateLimiter`` rather than relying on 429 retries.

Writes: gated behind ``AGENT_WRITES_ENABLED`` (security rule #1). While the gate
is closed, ``create_task`` records exactly what it would have sent and returns
``None`` instead of calling the API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import httpx

from integrations.amocrm.models import Lead, PipelineStage, PipelineSummary, Task
from integrations.common import divisions
from integrations.common.config import settings
from integrations.common.db import audited, log_action
from integrations.common.http import RateLimiter, request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.common.money import to_tiyin
from integrations.common.timeutil import UTC, last_24h_utc, now_utc

log = setup_logging("amocrm")

PAGE_LIMIT = 250  # amoCRM maximum rows per page
MAX_PAGES = 100


class AmoCRMError(RuntimeError):
    """Raised when amoCRM returns an unrecoverable error."""


class AmoCRMClient:
    """Async client for amoCRM API v4.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None
        self._limiter = RateLimiter(settings.amocrm_max_rps)
        self._user_names: dict[int, str] | None = None
        self._pipelines: dict[int, dict[str, Any]] | None = None

    async def __aenter__(self) -> "AmoCRMClient":
        """Open the HTTP client with the bearer token attached.

        Returns:
            The ready client.

        Raises:
            AmoCRMError: if the subdomain or token is unset.
        """
        if not settings.amocrm_subdomain or not settings.amocrm_long_lived_token.get_secret_value():
            raise AmoCRMError("amoCRM is not configured — fill AMOCRM_* in .env")

        self._client = httpx.AsyncClient(
            base_url=f"{settings.amocrm_base_url}/api/v4",
            timeout=httpx.Timeout(30.0),
            headers={
                "Authorization": f"Bearer {settings.amocrm_long_lived_token.get_secret_value()}",
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- requests

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None
    ) -> dict[str, Any] | None:
        """Issue one audited, rate-limited request.

        Args:
            method: HTTP verb.
            path: Path below ``/api/v4``, e.g. ``/leads``.
            params: Query parameters.
            json: Request body for writes.

        Returns:
            Parsed JSON body, or ``None`` for 204/empty responses.

        Raises:
            AmoCRMError: on a 4xx/5xx that survived retries.
        """
        assert self._client is not None
        mode = "read" if method == "GET" else "write"

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system="amocrm",
            run_id=self.run_id,
            target_ref=f"{method} {path}",
            mode=mode,
            payload={"params": params or {}},
        ) as ctx:
            response = await request_with_retry(
                self._client,
                method,
                path,
                params=params,
                json=json,
                rate_limiter=self._limiter,
            )
            ctx["http_status"] = response.status_code

            if response.status_code == 204 or not response.content:
                return None
            if response.status_code >= 400:
                raise AmoCRMError(f"amoCRM {method} {path} failed: HTTP {response.status_code} {response.text[:400]}")
            return response.json()

    async def _paginate(self, path: str, params: dict[str, Any], collection: str) -> list[dict[str, Any]]:
        """Walk amoCRM's paged list responses.

        Args:
            path: Entity path, e.g. ``/leads``.
            params: Query parameters (``limit`` and ``page`` are managed here).
            collection: Key inside ``_embedded`` holding the rows.

        Returns:
            All rows across pages. amoCRM answers 204 with no body when a page
            is empty, which ends the walk.

        Raises:
            AmoCRMError: on an API error.
        """
        rows: list[dict[str, Any]] = []
        page_params = dict(params)
        page_params["limit"] = PAGE_LIMIT

        for page in range(1, MAX_PAGES + 1):
            page_params["page"] = page
            body = await self._request("GET", path, params=page_params)
            if not body:
                break
            batch = body.get("_embedded", {}).get(collection, [])
            rows.extend(batch)
            if len(batch) < PAGE_LIMIT:
                break
            if page == MAX_PAGES:
                log.warning("amoCRM {} hit the {}-page cap — results may be truncated", path, MAX_PAGES)

        return rows

    # ----------------------------------------------------------------- queries

    async def get_leads(
        self,
        status: int | None = None,
        responsible_id: int | None = None,
        pipeline_id: int | None = None,
        include_closed: bool = False,
    ) -> list[Lead]:
        """Fetch deals, optionally filtered.

        Args:
            status: Restrict to one status (stage) id.
            responsible_id: Restrict to one responsible user id.
            pipeline_id: Restrict to one pipeline id.
            include_closed: Include won/lost deals. Off by default — the brief
                only cares about live pipeline.

        Returns:
            Normalized ``Lead`` objects with price in tiyin.

        Raises:
            AmoCRMError: on an API error.
        """
        params: dict[str, Any] = {"with": "contacts"}
        if status is not None:
            params["filter[statuses][0][status_id]"] = status
            params["filter[statuses][0][pipeline_id]"] = pipeline_id or 0
        if responsible_id is not None:
            params["filter[responsible_user_id]"] = responsible_id
        if pipeline_id is not None and status is None:
            params["filter[pipeline_id]"] = pipeline_id

        rows = await self._paginate("/leads", params, "leads")
        pipelines = await self._get_pipelines()
        users = await self._get_user_names()

        leads = [self._to_lead(row, pipelines, users) for row in rows]
        if not include_closed:
            leads = [lead for lead in leads if not lead.is_closed]

        log.info("amoCRM: {} lead(s) fetched", len(leads))
        return leads

    async def get_tasks(self, overdue: bool = False, responsible_id: int | None = None) -> list[Task]:
        """Fetch tasks.

        Args:
            overdue: Only tasks that are open and past their due time.
            responsible_id: Restrict to one responsible user.

        Returns:
            Normalized ``Task`` objects.

        Raises:
            AmoCRMError: on an API error.
        """
        params: dict[str, Any] = {"filter[is_completed]": 0}
        if overdue:
            params["filter[complete_till][to]"] = int(now_utc().timestamp())
        if responsible_id is not None:
            params["filter[responsible_user_id]"] = responsible_id

        rows = await self._paginate("/tasks", params, "tasks")
        tasks = [
            Task(
                id=row["id"],
                text=row.get("text"),
                entity_id=row.get("entity_id"),
                entity_type=row.get("entity_type"),
                responsible_user_id=row.get("responsible_user_id"),
                complete_till=_ts(row.get("complete_till")),
                is_completed=bool(row.get("is_completed")),
            )
            for row in rows
        ]
        log.info("amoCRM: {} task(s) fetched (overdue={})", len(tasks), overdue)
        return tasks

    async def create_task(
        self,
        lead_id: int,
        text: str,
        due_date: datetime,
        responsible_id: int | None = None,
        task_type_id: int = 1,
    ) -> int | None:
        """Create a task on a deal.

        Gated by ``AGENT_WRITES_ENABLED``. While the gate is closed the intended
        payload is audited as a dry run and nothing is sent.

        Args:
            lead_id: Deal the task attaches to.
            text: Task text shown to the manager.
            due_date: When the task is due (any timezone; sent as a UTC epoch).
            responsible_id: User the task is assigned to; defaults to the
                deal's own responsible user as configured in amoCRM.
            task_type_id: 1 = call, 2 = meeting (amoCRM defaults).

        Returns:
            The new task id, or ``None`` when writes are disabled.

        Raises:
            AmoCRMError: on an API error.
        """
        payload: dict[str, Any] = {
            "entity_id": lead_id,
            "entity_type": "leads",
            "text": text,
            "complete_till": int(due_date.astimezone(UTC).timestamp()),
            "task_type_id": task_type_id,
        }
        if responsible_id is not None:
            payload["responsible_user_id"] = responsible_id

        if not settings.agent_writes_enabled or settings.dry_run:
            log.info("[write gate closed] would create amoCRM task on lead {}: {}", lead_id, text)
            await log_action(
                agent=self.agent,
                action="create_task",
                target_system="amocrm",
                status="dry_run",
                run_id=self.run_id,
                target_ref=f"lead:{lead_id}",
                mode="write",
                payload=payload,
            )
            return None

        body = await self._request("POST", "/tasks", json=[payload])
        created = (body or {}).get("_embedded", {}).get("tasks", [])
        task_id = created[0]["id"] if created else None
        log.info("amoCRM task {} created on lead {}", task_id, lead_id)
        return task_id

    async def get_pipeline_summary(self) -> PipelineSummary:
        """Aggregate the whole open pipeline for the CEO brief.

        Returns:
            A ``PipelineSummary`` with per-stage totals, the deals that have no
            next task, and the count of leads created in the last 24 hours.

        Raises:
            AmoCRMError: on an API error.
        """
        leads = await self.get_leads()
        window_start, _ = last_24h_utc()

        stages: dict[tuple[int, int], PipelineStage] = {}
        summary = PipelineSummary()

        for lead in leads:
            key = (lead.pipeline_id or 0, lead.status_id or 0)
            stage = stages.get(key)
            if stage is None:
                stage = PipelineStage(
                    pipeline_id=lead.pipeline_id or 0,
                    pipeline_name=lead.pipeline_name,
                    status_id=lead.status_id or 0,
                    status_name=lead.status_name,
                    division=lead.division,
                )
                stages[key] = stage

            stage.deals_count += 1
            stage.deals_value_tiyin += lead.price_tiyin
            summary.total_deals += 1
            summary.total_value_tiyin += lead.price_tiyin

            if not lead.has_next_task:
                stage.deals_without_task += 1
                summary.deals_without_task.append(lead)

            if lead.created_at and lead.created_at >= window_start:
                summary.new_leads_24h += 1

        summary.stages = sorted(stages.values(), key=lambda s: (-s.deals_value_tiyin, s.status_id))
        summary.deals_without_task.sort(key=lambda l: l.price_tiyin, reverse=True)

        log.info(
            "amoCRM pipeline: {} deal(s), {} without a next task, {} new in 24h",
            summary.total_deals,
            len(summary.deals_without_task),
            summary.new_leads_24h,
        )
        return summary

    async def get_lead(self, lead_id: int) -> Lead | None:
        """Fetch a single deal by id.

        Args:
            lead_id: The deal id.

        Returns:
            The ``Lead``, or ``None`` if it no longer exists.

        Raises:
            AmoCRMError: on an API error other than 404.
        """
        try:
            body = await self._request("GET", f"/leads/{lead_id}")
        except AmoCRMError as err:
            if "HTTP 404" in str(err):
                return None
            raise
        if not body:
            return None
        return self._to_lead(body, await self._get_pipelines(), await self._get_user_names())

    # ----------------------------------------------------------------- helpers

    def _to_lead(
        self, row: dict[str, Any], pipelines: dict[int, dict[str, Any]], users: dict[int, str]
    ) -> Lead:
        """Map a raw amoCRM lead payload onto our ``Lead`` model."""
        pipeline_id = row.get("pipeline_id")
        status_id = row.get("status_id")
        pipeline = pipelines.get(pipeline_id or 0, {})
        responsible = row.get("responsible_user_id")

        return Lead(
            id=row["id"],
            name=row.get("name"),
            price_tiyin=to_tiyin(row.get("price") or 0),
            pipeline_id=pipeline_id,
            pipeline_name=pipeline.get("name"),
            status_id=status_id,
            status_name=pipeline.get("statuses", {}).get(status_id),
            responsible_user_id=responsible,
            responsible_user_name=users.get(responsible or 0),
            division=divisions.division_from_amocrm(pipeline_id),
            created_at=_ts(row.get("created_at")),
            updated_at=_ts(row.get("updated_at")),
            closest_task_at=_ts(row.get("closest_task_at")),
            # amoCRM marks won/lost stages with these reserved status ids.
            is_closed=status_id in (142, 143),
        )

    async def _get_pipelines(self) -> dict[int, dict[str, Any]]:
        """Fetch and cache pipeline and stage names for this process.

        Returns:
            ``{pipeline_id: {"name": str, "statuses": {status_id: name}}}``.
            An API failure degrades to an empty mapping so ids still work.
        """
        if self._pipelines is not None:
            return self._pipelines

        try:
            body = await self._request("GET", "/leads/pipelines")
        except AmoCRMError as err:
            log.warning("Could not load amoCRM pipelines: {}", err)
            self._pipelines = {}
            return self._pipelines

        pipelines: dict[int, dict[str, Any]] = {}
        for row in (body or {}).get("_embedded", {}).get("pipelines", []):
            statuses = {
                st["id"]: st.get("name")
                for st in row.get("_embedded", {}).get("statuses", [])
            }
            pipelines[row["id"]] = {"name": row.get("name"), "statuses": statuses}

        self._pipelines = pipelines
        return pipelines

    async def _get_user_names(self) -> dict[int, str]:
        """Fetch and cache amoCRM user names for this process.

        Returns:
            ``{user_id: display name}``; empty mapping if the call fails.
        """
        if self._user_names is not None:
            return self._user_names

        try:
            rows = await self._paginate("/users", {}, "users")
        except AmoCRMError as err:
            log.warning("Could not load amoCRM users: {}", err)
            self._user_names = {}
            return self._user_names

        self._user_names = {row["id"]: row.get("name", "") for row in rows}
        return self._user_names


def _ts(value: Any) -> datetime | None:
    """Convert an amoCRM unix timestamp to an aware UTC datetime.

    Args:
        value: Unix seconds, or ``None``.

    Returns:
        Aware UTC datetime, or ``None`` for missing/zero/invalid values.
    """
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (ValueError, OSError, TypeError):
        return None
