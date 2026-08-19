"""Microsoft Graph client using app-only (client credentials) auth.

The agents run unattended, so every call uses application permissions rather
than a signed-in user. Required app permissions, admin-consented in Azure:

    Group.ReadWrite.All      create the Command Center team and channels
    Team.Create              team creation
    Channel.Create           channel creation
    Tasks.Read.All           read Planner tasks (CEO brief)
    Tasks.ReadWrite.All      create Planner tasks (follow-up agent; needs the
                             AGENT_WRITES_ENABLED gate open)
    User.Read.All            resolve assignee display names

Tokens are cached in-process and refreshed a minute before expiry.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx

from integrations.common.config import settings
from integrations.common.db import audited, log_action
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import UTC, now_utc

log = setup_logging("msgraph")

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
STANDARD_TEAM_TEMPLATE = "https://graph.microsoft.com/v1.0/teamsTemplates('standard')"
MAX_PAGES = 50


class GraphError(RuntimeError):
    """Raised when Microsoft Graph returns an unrecoverable error."""


class GraphClient:
    """Async Microsoft Graph client with app-only authentication.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._user_names: dict[str, str] = {}

    async def __aenter__(self) -> "GraphClient":
        """Open the HTTP client and acquire the first token.

        Returns:
            The ready client.

        Raises:
            GraphError: if Azure app credentials are unset or rejected.
        """
        self._client = httpx.AsyncClient(base_url=GRAPH_ROOT, timeout=httpx.Timeout(60.0))
        await self._authenticate()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- auth

    async def _authenticate(self) -> None:
        """Acquire an app-only access token from Azure AD.

        Raises:
            GraphError: if the tenant/client configuration is missing or the
                token endpoint rejects the credentials.
        """
        if not (settings.ms_tenant_id and settings.ms_client_id):
            raise GraphError("Microsoft Graph is not configured — fill MS_* in .env")

        token_url = f"https://login.microsoftonline.com/{settings.ms_tenant_id}/oauth2/v2.0/token"
        form = {
            "client_id": settings.ms_client_id,
            "client_secret": settings.ms_client_secret.get_secret_value(),
            "scope": settings.ms_graph_scope,
            "grant_type": "client_credentials",
        }

        async with audited(
            agent=self.agent,
            action="authenticate",
            target_system="msgraph",
            run_id=self.run_id,
            target_ref="oauth2/token",
            payload={"tenant": settings.ms_tenant_id},
        ) as ctx:
            async with httpx.AsyncClient(timeout=30.0) as auth_client:
                response = await request_with_retry(auth_client, "POST", token_url, data=form)
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise GraphError(f"Graph token request failed: HTTP {response.status_code} {response.text[:300]}")

            body = response.json()
            self._token = body["access_token"]
            self._token_expires_at = now_utc() + timedelta(seconds=int(body.get("expires_in", 3600)) - 60)

        log.info("Graph token acquired (valid until {})", self._token_expires_at)

    async def _ensure_token(self) -> None:
        """Refresh the token if it is missing or about to expire."""
        if self._token is None or self._token_expires_at is None or now_utc() >= self._token_expires_at:
            await self._authenticate()

    # ------------------------------------------------------------- requests

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
    ) -> dict[str, Any] | None:
        """Issue one audited Graph request.

        Args:
            method: HTTP verb.
            path: Path below the Graph root, e.g. ``/groups``.
            params: OData query options.
            json: Request body.
            headers: Extra headers (e.g. ``If-Match`` for Planner updates).
            expected: Status codes treated as success.

        Returns:
            Parsed JSON body, or ``None`` for empty responses.

        Raises:
            GraphError: on an unexpected status code.
        """
        await self._ensure_token()
        assert self._client is not None
        mode = "read" if method == "GET" else "write"

        request_headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        request_headers.update(headers or {})

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system="msgraph",
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
                headers=request_headers,
                on_auth_failure=self._authenticate,
            )
            ctx["http_status"] = response.status_code
            if response.status_code not in expected:
                raise GraphError(
                    f"Graph {method} {path} failed: HTTP {response.status_code} {response.text[:400]}"
                )
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    async def _get_all(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Follow Graph's ``@odata.nextLink`` paging and return every row.

        Args:
            path: Collection path.
            params: OData query options for the first page.

        Returns:
            All items across pages.

        Raises:
            GraphError: on an API error.
        """
        rows: list[dict[str, Any]] = []
        body = await self._request("GET", path, params=params)

        for _ in range(MAX_PAGES):
            if not body:
                break
            rows.extend(body.get("value", []))
            next_link = body.get("@odata.nextLink")
            if not next_link:
                break
            body = await self._request("GET", next_link.replace(GRAPH_ROOT, ""))

        return rows

    # -------------------------------------------------------------- Planner

    async def get_planner_tasks(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """Fetch every Planner task across all plans owned by a group.

        Args:
            group_id: The M365 group behind the team; defaults to
                ``MS_PLANNER_GROUP_ID``.

        Returns:
            Task dicts enriched with ``planTitle`` and ``assignedToNames``.

        Raises:
            GraphError: on an API error, or if no group id is configured.
        """
        group = group_id or settings.ms_planner_group_id
        if not group:
            raise GraphError("MS_PLANNER_GROUP_ID is not set — run scripts/setup/create_teams_structure.py first")

        plans = await self._get_all(f"/groups/{group}/planner/plans")
        tasks: list[dict[str, Any]] = []

        for plan in plans:
            plan_tasks = await self._get_all(f"/planner/plans/{plan['id']}/tasks")
            for task in plan_tasks:
                task["planTitle"] = plan.get("title")
                task["assignedToNames"] = await self._resolve_assignees(task.get("assignments", {}))
                tasks.append(task)

        log.info("Graph: {} Planner task(s) across {} plan(s)", len(tasks), len(plans))
        return tasks

    async def get_overdue_planner_tasks(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """Return incomplete Planner tasks whose due date has passed.

        Args:
            group_id: The M365 group behind the team.

        Returns:
            Overdue tasks, most overdue first, each with ``daysOverdue``.

        Raises:
            GraphError: on an API error.
        """
        now = now_utc()
        overdue: list[dict[str, Any]] = []

        for task in await self.get_planner_tasks(group_id):
            due_raw = task.get("dueDateTime")
            if not due_raw or int(task.get("percentComplete", 0)) >= 100:
                continue
            due = _parse_graph_datetime(due_raw)
            if due is None or due >= now:
                continue
            task["dueAt"] = due
            task["daysOverdue"] = (now - due).days
            overdue.append(task)

        overdue.sort(key=lambda t: t["daysOverdue"], reverse=True)
        log.info("Graph: {} overdue Planner task(s)", len(overdue))
        return overdue

    async def create_planner_task(
        self,
        plan_id: str,
        title: str,
        *,
        bucket_id: str | None = None,
        due_date: datetime | None = None,
        assigned_user_id: str | None = None,
    ) -> str | None:
        """Create a Planner task.

        Gated by ``AGENT_WRITES_ENABLED`` (security rule #1): while the gate is
        closed the intended payload is audited as a dry run and nothing is sent.

        Args:
            plan_id: Target plan.
            title: Task title.
            bucket_id: Target bucket; the plan's default bucket if omitted.
            due_date: Due date/time (sent as UTC).
            assigned_user_id: Azure AD object id of the assignee.

        Returns:
            The new task id, or ``None`` when writes are disabled.

        Raises:
            GraphError: on an API error.
        """
        payload: dict[str, Any] = {"planId": plan_id, "title": title}
        if bucket_id:
            payload["bucketId"] = bucket_id
        if due_date:
            payload["dueDateTime"] = due_date.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if assigned_user_id:
            payload["assignments"] = {
                assigned_user_id: {
                    "@odata.type": "#microsoft.graph.plannerAssignment",
                    "orderHint": " !",
                }
            }

        if not settings.agent_writes_enabled or settings.dry_run:
            log.info("[write gate closed] would create Planner task '{}' in plan {}", title, plan_id)
            await log_action(
                agent=self.agent,
                action="create_planner_task",
                target_system="msgraph",
                status="dry_run",
                run_id=self.run_id,
                target_ref=plan_id,
                mode="write",
                payload=payload,
            )
            return None

        body = await self._request("POST", "/planner/tasks", json=payload, expected=(201,))
        task_id = (body or {}).get("id")
        log.info("Planner task {} created in plan {}", task_id, plan_id)
        return task_id

    async def get_plans_for_group(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """List the Planner plans owned by a group.

        Args:
            group_id: The M365 group; defaults to ``MS_PLANNER_GROUP_ID``.

        Returns:
            Plan dicts with ``id`` and ``title``.

        Raises:
            GraphError: on an API error, or if no group id is configured.
        """
        group = group_id or settings.ms_planner_group_id
        if not group:
            raise GraphError("MS_PLANNER_GROUP_ID is not set")
        return await self._get_all(f"/groups/{group}/planner/plans")

    # ---------------------------------------------------------------- Teams

    async def find_team_by_name(self, display_name: str) -> dict[str, Any] | None:
        """Look up a team by its display name.

        Args:
            display_name: Exact team name.

        Returns:
            The group object, or ``None`` if no team matches.

        Raises:
            GraphError: on an API error.
        """
        escaped = display_name.replace("'", "''")
        groups = await self._get_all(
            "/groups",
            {
                "$filter": f"displayName eq '{escaped}' and resourceProvisioningOptions/Any(x:x eq 'Team')",
                "$select": "id,displayName,mail",
                "$count": "true",
            },
        )
        return groups[0] if groups else None

    async def create_team(self, display_name: str, description: str, owner_upn: str) -> str:
        """Create a Microsoft Team with the standard template.

        App-only team creation requires an explicit owner, which is why
        ``owner_upn`` is mandatory rather than defaulted.

        Args:
            display_name: Team name.
            description: Team description.
            owner_upn: UPN (email) of the human owner.

        Returns:
            The team/group id.

        Raises:
            GraphError: if creation fails or the async operation never
                resolves to a team id.
        """
        owner = await self._request("GET", f"/users/{owner_upn}", params={"$select": "id,displayName"})
        owner_id = (owner or {}).get("id")
        if not owner_id:
            raise GraphError(f"Owner {owner_upn} not found in the tenant")

        payload = {
            "template@odata.bind": STANDARD_TEAM_TEMPLATE,
            "displayName": display_name,
            "description": description,
            "members": [
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{GRAPH_ROOT}/users('{owner_id}')",
                }
            ],
        }

        await self._ensure_token()
        assert self._client is not None
        response = await request_with_retry(
            self._client,
            "POST",
            "/teams",
            json=payload,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            on_auth_failure=self._authenticate,
        )
        if response.status_code not in (201, 202):
            raise GraphError(f"Team creation failed: HTTP {response.status_code} {response.text[:400]}")

        # 202 Accepted: the team id is in the Location header, teams/{id}/operations/{op}
        location = response.headers.get("Location", "")
        team_id = location.strip("/").split("/")[1] if "/" in location.strip("/") else None
        if not team_id:
            team_id = (response.json() or {}).get("id") if response.content else None
        if not team_id:
            raise GraphError("Team creation accepted but no team id was returned")

        log.info("Team '{}' created ({})", display_name, team_id)
        return team_id

    async def list_channels(self, team_id: str) -> list[dict[str, Any]]:
        """List a team's channels.

        Args:
            team_id: The team id.

        Returns:
            Channel dicts with ``id`` and ``displayName``.

        Raises:
            GraphError: on an API error.
        """
        return await self._get_all(f"/teams/{team_id}/channels", {"$select": "id,displayName"})

    async def create_channel(self, team_id: str, display_name: str, description: str = "") -> str | None:
        """Create a standard channel in a team.

        Args:
            team_id: Target team.
            display_name: Channel name.
            description: Channel description.

        Returns:
            The new channel id, or ``None`` if Graph returned no body.

        Raises:
            GraphError: on an API error.
        """
        body = await self._request(
            "POST",
            f"/teams/{team_id}/channels",
            json={
                "@odata.type": "#Microsoft.Graph.channel",
                "displayName": display_name,
                "description": description,
                "membershipType": "standard",
            },
            expected=(201, 202),
        )
        return (body or {}).get("id")

    # -------------------------------------------------------------- helpers

    async def _resolve_assignees(self, assignments: dict[str, Any]) -> list[str]:
        """Turn Planner assignment user ids into display names.

        Args:
            assignments: The task's ``assignments`` map.

        Returns:
            Display names; falls back to the raw id when lookup fails.
        """
        names: list[str] = []
        for user_id in assignments:
            if user_id not in self._user_names:
                try:
                    user = await self._request("GET", f"/users/{user_id}", params={"$select": "displayName"})
                    self._user_names[user_id] = (user or {}).get("displayName", user_id)
                except GraphError:
                    self._user_names[user_id] = user_id
            names.append(self._user_names[user_id])
        return names


def _parse_graph_datetime(value: str | None) -> datetime | None:
    """Parse a Graph ISO-8601 timestamp into an aware UTC datetime.

    Args:
        value: e.g. ``"2026-08-18T17:00:00Z"``.

    Returns:
        Aware UTC datetime, or ``None`` if unparseable.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
