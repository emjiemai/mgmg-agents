"""BILLZ 2.0 public API — read-only client for the shop reports.

Docs: https://docs.billz.io (OpenAPI: https://docs.billz.io/openapi.yaml),
read 2026-10-01. What this relies on, as documented there:

  * ``POST /v1/auth/login`` with ``{"secret_token": <integration key>}`` →
    ``data.access_token`` (15 days). The key is made in BILLZ: Настройки →
    Компания → Ключи интеграции (docs: start/integration-key).
  * Every other call: ``Authorization: Bearer <access_token>``. A 401 means
    the token expired: log in again with the key and repeat once. (The
    refresh token is deliberately NOT used — reusing a spent one revokes the
    whole session family, and every cron run is a fresh process anyway.)
  * ``GET /v1/shop``, ``GET /v1/general-report-table`` (shop × day),
    ``GET /v1/seller-general-table``, ``GET /v1/product-general-table`` —
    all in the integration key's default route list. Array parameters are
    comma-separated (``explode: false``); dates ``YYYY-MM-DD`` in the
    company's time zone (Tashkent); amounts in whole currency units.
  * 2 requests per second per IP; 429 = wait and retry (request_with_retry
    does, with backoff), so pages are fetched with a short pause between.

Read-only by design: no sale, transfer or catalogue change is ever sent.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date
from typing import Any

import httpx

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging

log = setup_logging("billz")

BASE_URL = "https://api-admin.billz.ai"  # the documented production server
PAGE = 100
MAX_PAGES = 50
PAUSE = 0.6  # seconds between pages: the documented limit is 2 requests/second


class BillzError(RuntimeError):
    """BILLZ could not be read (not configured, refused, or answered nonsense)."""


def _day(d: date) -> str:
    return d.strftime("%Y-%m-%d")


class BillzClient:
    """Reads BILLZ reports.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
        transport: Test hook — an ``httpx`` transport instead of the network.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None

    async def __aenter__(self) -> "BillzClient":
        if not settings.billz_configured:
            raise BillzError("BILLZ is not configured (BILLZ_SECRET_TOKEN)")
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=60.0, transport=self._transport)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def _login(self) -> None:
        assert self._http is not None
        response = await request_with_retry(
            self._http, "POST", "/v1/auth/login",
            json={"secret_token": settings.billz_secret_token.get_secret_value()},
        )
        body = _body(response, "/v1/auth/login")
        token = ((body or {}).get("data") or {}).get("access_token") if isinstance(body, dict) else None
        if not token:
            raise BillzError("/v1/auth/login: no access_token in the answer")
        self._token = str(token)

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        assert self._http is not None
        if self._token is None:
            await self._login()
        for attempt in (1, 2):
            response = await request_with_retry(
                self._http, "GET", path, params=params, headers={"Authorization": f"Bearer {self._token}"}
            )
            if response.status_code == 401 and attempt == 1:
                await self._login()  # the 15-day token ran out: one fresh login, then repeat
                continue
            return _body(response, path)
        return None  # pragma: no cover — the loop always returns

    async def _pages(self, path: str, params: dict[str, Any], key: str) -> list[dict[str, Any]]:
        """Every page of a paged report (``count`` + a list under ``key``)."""
        async with audited(agent=self.agent, action="report", target_system="billz", run_id=self.run_id,
                           target_ref=path) as ctx:
            rows: list[dict[str, Any]] = []
            for page in range(1, MAX_PAGES + 1):
                if page > 1:
                    await asyncio.sleep(PAUSE)
                body = await self._get(path, {**params, "page": page, "limit": PAGE})
                if not isinstance(body, dict):
                    raise BillzError(f"{path}: unexpected answer shape")
                batch = body.get(key) or []
                rows.extend(batch)
                total = int(body.get("count") or 0)
                if not batch or len(rows) >= total or len(batch) < PAGE:
                    ctx["payload"].update(pages=page, rows=len(rows))
                    return rows
            raise BillzError(f"{path}: more than {MAX_PAGES} pages")

    async def shops(self) -> list[dict[str, Any]]:
        return await self._pages("/v1/shop", {}, "shops")

    async def shop_days(self, start: date, end: date) -> list[dict[str, Any]]:
        """Revenue per shop per day (``GET /v1/general-report-table``)."""
        return await self._pages(
            "/v1/general-report-table",
            {"start_date": _day(start), "end_date": _day(end), "detalization": "day", "currency": "UZS"},
            "shop_stats_by_date",
        )

    async def sellers(self, start: date, end: date) -> list[dict[str, Any]]:
        """Sales per seller over the period, all shops together."""
        return await self._pages(
            "/v1/seller-general-table",
            {"start_date": _day(start), "end_date": _day(end), "detalization": "month", "currency": "UZS",
             "hide_sellers_without_sales": "true", "group_without_shops": "true"},
            "seller_stats_by_date",
        )

    async def positions(self, day: date, shop_ids: list[str]) -> list[dict[str, Any]]:
        """Every cheque line of one day (``product-general-table`` by position).

        Each row is one sold or returned product of one cheque: ``order_id`` /
        ``order_number``, ``shop_name``, ``product_sku`` / ``product_barcode``
        / ``product_name``, ``net_sold_measurement_value``, ``net_sales``
        (so'm, after discounts and returns), ``seller_full_name``. Asked one
        day at a time so each line's day is known for certain.
        """
        return await self._pages(
            "/v1/product-general-table",
            {"start_date": _day(day), "end_date": _day(day), "shop_ids": ",".join(shop_ids), "currency": "UZS",
             "detalization": "day", "detalization_by_position": "true"},
            "products_stats_by_date",
        )

    async def products(self, start: date, end: date, shop_ids: list[str]) -> list[dict[str, Any]]:
        """Sales per product over the period, all shops together (shop_ids is required here)."""
        return await self._pages(
            "/v1/product-general-table",
            {"start_date": _day(start), "end_date": _day(end), "shop_ids": ",".join(shop_ids), "currency": "UZS",
             "group_without_shops": "true"},
            "products_stats_by_date",
        )


def _body(response: httpx.Response, path: str) -> Any:
    """The JSON answer, or a readable error (never the key)."""
    if response.status_code >= 400:
        raise BillzError(f"{path}: HTTP {response.status_code} — {response.text[:200]}")
    try:
        return json.loads(response.text)
    except ValueError as exc:
        raise BillzError(f"{path}: not JSON — {response.text[:200]}") from exc
