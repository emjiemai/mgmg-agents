"""Verifix public API v1 — read-only client (attendance).

Docs: https://documenter.getpostman.com/view/23097602/2s83ziN3VR ("Verifix
Public API v1.0", sent by Verifix support 2026-09-28). What this uses, as
documented there:

  * ``POST /security/oauth/token`` — client_credentials -> ``access_token``,
    valid ``expires_in`` seconds (10800 in their example). The client id and
    secret come from Verifix: Администрирование -> Настройки -> Внешние
    системы -> Клиенты OAuth2 для сервера для компании (see
    docs/agent-specs/11-attendance.md).
  * ``POST /b/vhr/api/v1/core/timesheet$export`` — per employee, per day: the
    schedule's start and end, first arrival, last departure, and the day's
    time facts (lateness, absence, sick leave, vacation...).
  * ``POST /b/vhr/api/v1/core/time_kind$list`` — the names of those facts.

Two ways in, as the docs give them (settings.verifix_auth picks):
  * **oauth** — client credentials → bearer token (above). ``filial_id`` only
    if set: the client is already tied to one organisation.
  * **basic** — "Basic auth (deprecated)": ``Authorization: Basic
    base64("<user>@<company>:<password>")`` on every call, and then the
    ``filial_id`` header (the organisation's ID) is required. Used when the
    company can't create an OAuth client (2026-10-01).
Every call also sends ``project_code: vhr``.
Lists page with a ``cursor`` request header and ``meta.next_cursor`` in the
answer (-1 = no more). Verifix sends numbers as strings and labels some JSON
as text/plain, so bodies are read as JSON whatever the header says.

Read-only by design: nothing here writes to Verifix.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import date
from typing import Any

import httpx

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging

log = setup_logging("verifix")

# The one documented host. Deliberately not a setting (see config.py).
BASE_URL = "https://app.verifix.com"
TOKEN_PATH = "/security/oauth/token"
TIMESHEET_PATH = "/b/vhr/api/v1/core/timesheet$export"
TIME_KINDS_PATH = "/b/vhr/api/v1/core/time_kind$list"
FILIAL_INFO_PATH = "/b/vhr/api/v1/core/filial$info"
PROJECT_CODE = "vhr"
TIMESHEET_PAGE = 100  # the documented maximum for timesheet$export
LIST_PAGE = 500  # ...and for the other lists
MAX_PAGES = 50


class VerifixError(RuntimeError):
    """Verifix could not be read (not configured, refused, or answered nonsense)."""


def _day(d: date) -> str:
    return d.strftime("%d.%m.%Y")


class VerifixClient:
    """Reads attendance from Verifix.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
        transport: Test hook — an ``httpx`` transport instead of the network.
    """

    def __init__(
        self,
        agent: str = "-",
        run_id: uuid.UUID | str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.agent = agent
        self.run_id = run_id
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires = 0.0

    async def __aenter__(self) -> "VerifixClient":
        if not settings.verifix_configured:
            raise VerifixError("Verifix is not configured (VERIFIX_CLIENT_ID / VERIFIX_CLIENT_SECRET)")
        self._http = httpx.AsyncClient(
            base_url=BASE_URL, timeout=30.0, transport=self._transport
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------ auth

    async def _fetch_token(self) -> None:
        assert self._http is not None
        response = await request_with_retry(
            self._http,
            "POST",
            TOKEN_PATH,
            json={
                "grant_type": "client_credentials",
                "client_id": settings.verifix_client_id,
                "client_secret": settings.verifix_client_secret.get_secret_value(),
                "scope": "read",
            },
        )
        data = _body(response, TOKEN_PATH)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise VerifixError(f"{TOKEN_PATH}: no access_token in the answer")
        self._token = str(token)
        # Renew a minute early rather than find out mid-run.
        self._token_expires = time.monotonic() + max(int(data.get("expires_in") or 600) - 60, 30)

    async def _headers(self, cursor: str | None, limit: int | None) -> dict[str, str]:
        if settings.verifix_auth == "basic":
            pair = f"{settings.verifix_login.strip()}:{settings.verifix_password.get_secret_value()}"
            authorization = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
        else:
            if self._token is None or time.monotonic() >= self._token_expires:
                await self._fetch_token()
            authorization = f"Bearer {self._token}"
        headers = {"project_code": PROJECT_CODE, "Authorization": authorization}
        if settings.verifix_filial_id:
            headers["filial_id"] = settings.verifix_filial_id
        if cursor is not None:
            headers["cursor"] = cursor
        if limit is not None:
            headers["limit"] = str(limit)
        return headers

    # ----------------------------------------------------------------- lists

    async def _page(self, path: str, body: dict[str, Any], cursor: str | None, limit: int) -> Any:
        assert self._http is not None
        response = await request_with_retry(
            self._http, "POST", path, json=body, headers=await self._headers(cursor, limit)
        )
        if response.status_code == 401 and settings.verifix_auth == "oauth":
            # The token expired early or was revoked: one more try with a fresh one.
            # (A 401 with login+password means wrong credentials: no point retrying.)
            self._token = None
            response = await request_with_retry(
                self._http, "POST", path, json=body, headers=await self._headers(cursor, limit)
            )
        return _body(response, path)

    async def _list(self, path: str, body: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """Every page of a list method."""
        async with audited(
            agent=self.agent, action="list", target_system="verifix", run_id=self.run_id, target_ref=path
        ) as ctx:
            rows: list[dict[str, Any]] = []
            cursor: str | None = None
            for page_number in range(1, MAX_PAGES + 1):
                data = await self._page(path, body, cursor, limit)
                page = (data.get("data") if isinstance(data, dict) else data) or []
                if not isinstance(page, list):
                    raise VerifixError(f"{path}: unexpected answer shape")
                rows.extend(page)
                meta = data.get("meta") if isinstance(data, dict) else None
                following = str((meta or {}).get("next_cursor", "-1"))
                if not page or following in ("-1", "", "None") or following == cursor:
                    ctx["payload"].update(pages=page_number, rows=len(rows))
                    return rows
                cursor = following
            raise VerifixError(f"{path}: more than {MAX_PAGES} pages")

    async def timesheet(self, begin: date, end: date) -> list[dict[str, Any]]:
        """Attendance per employee and day, ``begin``..``end`` inclusive (Tashkent dates)."""
        body = {"period_begin_date": _day(begin), "period_end_date": _day(end), "division_ids": [], "employee_ids": []}
        return await self._list(TIMESHEET_PATH, body, TIMESHEET_PAGE)

    async def organisation(self) -> dict[str, Any]:
        """The organisation the requests go to (``core/filial$info``) — its name, to check filial_id."""
        assert self._http is not None
        async with audited(agent=self.agent, action="filial_info", target_system="verifix", run_id=self.run_id,
                           target_ref=FILIAL_INFO_PATH):
            response = await request_with_retry(
                self._http, "POST", FILIAL_INFO_PATH, json={}, headers=await self._headers(None, None)
            )
            body = _body(response, FILIAL_INFO_PATH)
        return body if isinstance(body, dict) else {}

    async def time_kinds(self) -> list[dict[str, Any]]:
        """The company's time kinds (Явка, Опоздание, Больничный...)."""
        return await self._list(TIME_KINDS_PATH, {"time_kind_ids": []}, LIST_PAGE)


def _body(response: httpx.Response, path: str) -> Any:
    """The JSON answer, or a readable error (never the secret)."""
    if response.status_code >= 400:
        raise VerifixError(f"{path}: HTTP {response.status_code} — {response.text[:200]}")
    try:
        return json.loads(response.text)
    except ValueError as exc:
        raise VerifixError(f"{path}: not JSON — {response.text[:200]}") from exc
