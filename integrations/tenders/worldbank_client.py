"""World Bank Projects API client.

Verified live on 2026-08-19: ``GET https://search.worldbank.org/api/v3/projects
?format=json&rows=N`` — public, no API key, no auth. Response shape is
unusual: ``projects`` is a dict keyed by project id, not a list.

ADB (Asian Development Bank) is a separate institution with its own data
portal and is NOT covered by this client — the n8n workflow's "World Bank +
ADB Projects" node needs its exact ADB URL confirmed before that half can be
built; see integrations/tenders/README.md.
"""

from __future__ import annotations

import uuid

import httpx

from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import parse_sap_date  # generic YYYY-MM-DD[Thh:mm:ssZ] parser
from integrations.search.models import RawLead

log = setup_logging("worldbank")

BASE_URL = "https://search.worldbank.org/api/v3/projects"
PROJECT_URL_TEMPLATE = "https://projects.worldbank.org/en/projects-operations/project-detail/{id}"


class WorldBankError(RuntimeError):
    """Raised when the World Bank API returns an error the client cannot recover from."""


class WorldBankClient:
    """Async client for the World Bank Projects search API.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "WorldBankClient":
        """Open the HTTP client (no auth required).

        Returns:
            The ready client.
        """
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def recent_projects(self, rows: int = 20, country: str | None = None) -> list[RawLead]:
        """Fetch recently approved World Bank projects.

        Args:
            rows: Max projects to fetch.
            country: Optional country short name filter (e.g. 'Uzbekistan').

        Returns:
            Normalized ``RawLead`` objects, newest board-approval date first
            (the API's own default sort).

        Raises:
            WorldBankError: on a non-200 response.
        """
        assert self._client is not None
        params: dict[str, object] = {"format": "json", "rows": rows}
        if country:
            params["countryshortname_exact"] = country

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system="worldbank",
            run_id=self.run_id,
            target_ref=BASE_URL,
            payload={"rows": rows, "country": country},
        ) as ctx:
            response = await request_with_retry(self._client, "GET", BASE_URL, params=params)
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise WorldBankError(f"World Bank API failed: HTTP {response.status_code} {response.text[:300]}")
            body = response.json()
            projects = body.get("projects", {})
            ctx["payload"]["results"] = len(projects)

        leads = [_to_lead(project_id, data) for project_id, data in projects.items()]
        log.info("World Bank: {} project(s) fetched", len(leads))
        return leads


def _to_lead(project_id: str, data: dict) -> RawLead:
    """Map one raw World Bank project record onto a ``RawLead``.

    Args:
        project_id: The dict key from the API response (e.g. 'P518248').
        data: The project's fields.

    Returns:
        A normalized lead, snippet built from borrower/sector/amount.
    """
    amount = data.get("totalamt") or data.get("lendprojectcost") or "0"
    borrower = data.get("borrower", "").strip()
    sector = data.get("regionname", "")
    snippet = f"{borrower} — {sector} — ${amount}" if borrower else sector

    return RawLead(
        source="worldbank",
        title=data.get("project_name", project_id),
        url=PROJECT_URL_TEMPLATE.format(id=project_id),
        snippet=snippet or None,
        published_at=parse_sap_date(data.get("boardapprovaldate")),
        extra={
            "project_id": project_id,
            "country": data.get("countryshortname"),
            "amount_usd": amount,
            "closing_date": data.get("closingdate"),
        },
    )
