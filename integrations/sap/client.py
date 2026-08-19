"""SAP Business One Service Layer client.

Read-only by construction: every request except Login/Logout is forced through
a GET guard, so the client physically cannot write to SAP during the 90-day
read-only period (security rule #1). Every call is audited to
``agent_actions`` (security rule #2).

Session handling: the Service Layer issues a ``B1SESSION`` cookie with a
timeout (30 minutes by default). We track expiry locally and re-login
pre-emptively; if the server expires a session early we also recover from the
resulting 401 via the retry layer's ``on_auth_failure`` hook.

Usage:
    async with SAPClient(agent="ceo-daily-brief", run_id=run_id) as sap:
        aging = await sap.get_ar_aging()
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import httpx

from integrations.common import divisions
from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.common.money import to_tiyin
from integrations.common.timeutil import days_between, now_utc, parse_sap_date, today_local
from integrations.sap.models import ARAging, ARInvoice, CashAccount, SalesSummary, StockItem

log = setup_logging("sap")

# Service Layer returns 20 rows per page by default; this header raises it.
PAGE_SIZE = 200
MAX_PAGES = 200  # hard stop: 40k rows, far above any realistic open-AR volume

# --- Field mapping notes -----------------------------------------------------
# The filters below target a stock SAP B1 chart of accounts. VERIFY against the
# live company database before first production run:
#   * CASH_ACCOUNT_CODES — set explicitly to the real bank/cash G/L codes.
#     When empty, the client falls back to the ChartOfAccounts 'cash account'
#     flag, which some installations do not maintain.
#   * BANK_NAME_BY_ACCOUNT — labels used in the CEO brief.
CASH_ACCOUNT_CODES: list[str] = []
BANK_NAME_BY_ACCOUNT: dict[str, str] = {
    # "5110": "Kapital Bank",
    # "5120": "Asia Alliance Bank",
}


class SAPError(RuntimeError):
    """Raised when SAP returns an error the client cannot recover from."""


class SAPClient:
    """Async client for the SAP B1 Service Layer.

    Args:
        agent: Name of the calling agent, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._base_url = settings.sap_base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._session_expires_at: datetime | None = None

    # ------------------------------------------------------------------ setup

    async def __aenter__(self) -> "SAPClient":
        """Open the HTTP client and authenticate.

        Returns:
            The connected client.

        Raises:
            SAPError: if login fails or credentials are unset.
        """
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(settings.sap_timeout_seconds),
            verify=settings.sap_verify_ssl,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        await self.login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Log out and close the HTTP client, ignoring logout failures."""
        if self._client is not None:
            try:
                await self._client.post("/Logout")
            except httpx.HTTPError as err:
                log.debug("SAP logout failed (session will expire on its own): {}", err)
            await self._client.aclose()
            self._client = None

    async def login(self) -> None:
        """Authenticate against the Service Layer and store the session cookie.

        Called on entry and again whenever the session is near expiry or the
        server rejects a request with 401.

        Raises:
            SAPError: if SAP rejects the credentials or is unreachable.
        """
        if not settings.sap_username or not settings.sap_base_url:
            raise SAPError("SAP credentials are not configured — fill SAP_* in .env")

        assert self._client is not None
        payload = {
            "CompanyDB": settings.sap_company_db,
            "UserName": settings.sap_username,
            "Password": settings.sap_password.get_secret_value(),
        }

        async with audited(
            agent=self.agent,
            action="login",
            target_system="sap",
            run_id=self.run_id,
            target_ref="/Login",
            payload={"company_db": settings.sap_company_db, "user": settings.sap_username},
        ) as ctx:
            response = await request_with_retry(self._client, "POST", "/Login", json=payload)
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise SAPError(f"SAP login failed: HTTP {response.status_code} {_error_text(response)}")

            body = response.json()
            timeout_minutes = int(body.get("SessionTimeout", 30))
            # Renew a minute early so a long page loop never trips over expiry.
            self._session_expires_at = now_utc() + timedelta(minutes=max(timeout_minutes - 1, 1))
            ctx["payload"]["session_timeout_minutes"] = timeout_minutes

        log.info("SAP session established (expires {})", self._session_expires_at)

    # ---------------------------------------------------------------- requests

    async def _ensure_session(self) -> None:
        """Re-login if the tracked session is missing or about to expire."""
        if self._session_expires_at is None or now_utc() >= self._session_expires_at:
            log.debug("SAP session expired or unknown — re-authenticating")
            await self.login()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue one audited GET against the Service Layer.

        Args:
            path: Entity path, e.g. ``/Invoices``.
            params: OData query options ($select, $filter, $orderby…).

        Returns:
            The parsed JSON body.

        Raises:
            SAPError: on a non-200 response.
            httpx.HTTPError: if all retries fail at the transport level.
        """
        await self._ensure_session()
        assert self._client is not None

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system="sap",
            run_id=self.run_id,
            target_ref=path,
            mode="read",
            payload={"params": _redact_params(params)},
        ) as ctx:
            response = await request_with_retry(
                self._client,
                "GET",
                path,
                params=params,
                headers={"Prefer": f"odata.maxpagesize={PAGE_SIZE}"},
                on_auth_failure=self.login,
            )
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise SAPError(f"SAP GET {path} failed: HTTP {response.status_code} {_error_text(response)}")
            body = response.json()
            ctx["payload"]["rows"] = len(body.get("value", []))
            return body

    async def _get_all(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Follow OData paging and return every row.

        Args:
            path: Entity path, e.g. ``/Invoices``.
            params: OData query options applied to the first page.

        Returns:
            All rows across pages, capped at ``MAX_PAGES`` pages.

        Raises:
            SAPError: on a non-200 response from any page.
        """
        rows: list[dict[str, Any]] = []
        page_params = dict(params or {})
        skip = 0

        for page in range(MAX_PAGES):
            if skip:
                page_params["$skip"] = skip
            body = await self._get(path, page_params)
            batch = body.get("value", [])
            rows.extend(batch)

            if "odata.nextLink" not in body and "@odata.nextLink" not in body:
                break
            if not batch:
                break
            skip += len(batch)
            if page == MAX_PAGES - 1:
                log.warning("SAP {} hit the {}-page cap — results may be truncated", path, MAX_PAGES)

        return rows

    # ----------------------------------------------------------------- queries

    async def get_cash_balance(self) -> list[CashAccount]:
        """Fetch current balances of the cash and bank G/L accounts.

        Uses ``CASH_ACCOUNT_CODES`` when configured; otherwise falls back to the
        chart-of-accounts cash flag.

        Returns:
            One ``CashAccount`` per account, balances in tiyin.

        Raises:
            SAPError: if SAP rejects the query.
        """
        if CASH_ACCOUNT_CODES:
            codes = " or ".join(f"Code eq '{code}'" for code in CASH_ACCOUNT_CODES)
            odata_filter = f"({codes})"
        else:
            log.warning(
                "CASH_ACCOUNT_CODES is empty — falling back to the SAP cash-account flag. "
                "Set the real G/L codes in integrations/sap/client.py before go-live."
            )
            odata_filter = "AccountType eq 'at_Other' and CashAccount eq 'tYES'"

        rows = await self._get_all(
            "/ChartOfAccounts",
            {
                "$select": "Code,Name,AccountCurrency,Balance,CashAccount",
                "$filter": odata_filter,
            },
        )

        accounts = [
            CashAccount(
                account_code=str(row.get("Code", "")),
                account_name=row.get("Name"),
                bank_name=BANK_NAME_BY_ACCOUNT.get(str(row.get("Code", ""))),
                currency=row.get("AccountCurrency") or settings.default_currency,
                balance_tiyin=to_tiyin(row.get("Balance")),
            )
            for row in rows
        ]
        log.info("SAP cash balance: {} account(s)", len(accounts))
        return accounts

    async def get_ar_aging(self, as_of: date | None = None) -> ARAging:
        """Pull all open A/R invoices and bucket them by days overdue.

        Args:
            as_of: Date to age against; defaults to today in Tashkent.

        Returns:
            An ``ARAging`` with per-invoice detail and bucket totals in tiyin.

        Raises:
            SAPError: if SAP rejects the query.
        """
        as_of = as_of or today_local()
        rows = await self._get_all(
            "/Invoices",
            {
                "$select": (
                    "DocEntry,DocNum,CardCode,CardName,DocDate,DocDueDate,"
                    "DocTotal,PaidToDate,DocCurrency,SalesPersonCode,DocumentStatus"
                ),
                "$filter": "DocumentStatus eq 'bost_Open' and Cancelled eq 'tNO'",
                "$orderby": "DocDueDate asc",
            },
        )

        sales_people = await self._get_sales_person_names(
            {int(r["SalesPersonCode"]) for r in rows if r.get("SalesPersonCode") not in (None, -1)}
        )

        invoices: list[ARInvoice] = []
        for row in rows:
            due = parse_sap_date(row.get("DocDueDate"))
            overdue = max(days_between(due, as_of), 0)
            total = to_tiyin(row.get("DocTotal"))
            paid = to_tiyin(row.get("PaidToDate"))
            balance = total - paid
            if balance <= 0:
                continue  # fully settled but not yet closed in SAP

            code = row.get("SalesPersonCode")
            code = int(code) if isinstance(code, (int, float)) and int(code) != -1 else None

            invoices.append(
                ARInvoice(
                    doc_entry=int(row["DocEntry"]),
                    doc_num=row.get("DocNum"),
                    card_code=row.get("CardCode", ""),
                    card_name=row.get("CardName"),
                    doc_date=parse_sap_date(row.get("DocDate")),
                    due_date=due,
                    days_overdue=overdue,
                    aging_bucket=aging_bucket(overdue),
                    currency=row.get("DocCurrency") or settings.default_currency,
                    doc_total_tiyin=total,
                    paid_to_date_tiyin=paid,
                    balance_due_tiyin=balance,
                    sales_person_code=code,
                    sales_person_name=sales_people.get(code) if code else None,
                    division=divisions.division_from_sap(code),
                )
            )

        aging = ARAging(snapshot_date=as_of, invoices=invoices)
        aging.total_open_tiyin = sum(i.balance_due_tiyin for i in invoices)
        aging.total_overdue_tiyin = sum(i.balance_due_tiyin for i in invoices if i.days_overdue > 0)
        for bucket in ("current", "1_30", "31_60", "61_90", "90_plus"):
            in_bucket = [i for i in invoices if i.aging_bucket == bucket]
            aging.bucket_totals_tiyin[bucket] = sum(i.balance_due_tiyin for i in in_bucket)
            aging.bucket_counts[bucket] = len(in_bucket)

        log.info(
            "SAP AR aging: {} open invoice(s), {} overdue",
            len(invoices),
            aging.overdue_count,
        )
        return aging

    async def get_overdue_invoices(self, min_days_overdue: int = 1) -> list[ARInvoice]:
        """Return open invoices past due by at least ``min_days_overdue`` days.

        Args:
            min_days_overdue: Threshold in days (1 = anything past due).

        Returns:
            Overdue invoices, largest balance first.

        Raises:
            SAPError: if SAP rejects the query.
        """
        aging = await self.get_ar_aging()
        overdue = [i for i in aging.invoices if i.days_overdue >= min_days_overdue]
        return sorted(overdue, key=lambda i: i.balance_due_tiyin, reverse=True)

    async def get_sales_summary(
        self, period_start: date | None = None, period_end: date | None = None
    ) -> SalesSummary:
        """Aggregate posted A/R invoices over a period.

        Args:
            period_start: First day, inclusive. Defaults to the 1st of the
                current Tashkent month.
            period_end: Last day, inclusive. Defaults to today.

        Returns:
            A ``SalesSummary`` with totals in tiyin and a per-division split.

        Raises:
            SAPError: if SAP rejects the query.
        """
        today = today_local()
        period_start = period_start or today.replace(day=1)
        period_end = period_end or today

        rows = await self._get_all(
            "/Invoices",
            {
                "$select": "DocEntry,DocTotal,DocDate,SalesPersonCode,DocCurrency",
                "$filter": (
                    f"DocDate ge '{period_start.isoformat()}' "
                    f"and DocDate le '{period_end.isoformat()}' "
                    "and Cancelled eq 'tNO'"
                ),
            },
        )

        summary = SalesSummary(period_start=period_start, period_end=period_end, invoices_count=len(rows))
        for row in rows:
            amount = to_tiyin(row.get("DocTotal"))
            summary.gross_total_tiyin += amount
            code = row.get("SalesPersonCode")
            code = int(code) if isinstance(code, (int, float)) and int(code) != -1 else None
            key = divisions.division_from_sap(code) or "unmapped"
            summary.by_division[key] = summary.by_division.get(key, 0) + amount

        log.info(
            "SAP sales {}..{}: {} invoice(s)", period_start, period_end, summary.invoices_count
        )
        return summary

    async def get_stock_levels(self, item_codes: Iterable[str] | None = None) -> list[StockItem]:
        """Fetch stock positions for inventory items.

        Args:
            item_codes: Restrict to these item codes; all stock items if None.

        Returns:
            One ``StockItem`` per item, with available = in stock - committed.

        Raises:
            SAPError: if SAP rejects the query.
        """
        odata_filter = "InventoryItem eq 'tYES'"
        if item_codes:
            joined = " or ".join(f"ItemCode eq '{code}'" for code in item_codes)
            odata_filter = f"{odata_filter} and ({joined})"

        rows = await self._get_all(
            "/Items",
            {
                "$select": "ItemCode,ItemName,QuantityOnStock,QuantityOrderedFromVendors,QuantityOrderedByCustomers",
                "$filter": odata_filter,
            },
        )

        items: list[StockItem] = []
        for row in rows:
            in_stock = float(row.get("QuantityOnStock") or 0)
            committed = float(row.get("QuantityOrderedByCustomers") or 0)
            items.append(
                StockItem(
                    item_code=row.get("ItemCode", ""),
                    item_name=row.get("ItemName"),
                    in_stock=in_stock,
                    committed=committed,
                    ordered=float(row.get("QuantityOrderedFromVendors") or 0),
                    available=in_stock - committed,
                )
            )

        log.info("SAP stock: {} item(s)", len(items))
        return items

    # ----------------------------------------------------------------- helpers

    async def _get_sales_person_names(self, codes: set[int]) -> dict[int, str]:
        """Resolve sales employee codes to names.

        Args:
            codes: SlpCode values appearing on the documents just fetched.

        Returns:
            Mapping of code to name; empty when ``codes`` is empty. Lookup
            failures degrade to an empty mapping rather than failing the run.
        """
        if not codes:
            return {}
        try:
            rows = await self._get_all(
                "/SalesPersons",
                {"$select": "SalesEmployeeCode,SalesEmployeeName"},
            )
        except (SAPError, httpx.HTTPError) as err:
            log.warning("Could not resolve sales person names: {}", err)
            return {}

        return {
            int(r["SalesEmployeeCode"]): r.get("SalesEmployeeName", "")
            for r in rows
            if r.get("SalesEmployeeCode") is not None
        }


def aging_bucket(days_overdue: int) -> str:
    """Classify an invoice into an aging bucket.

    Args:
        days_overdue: Days past the due date (0 or negative = not yet due).

    Returns:
        One of 'current', '1_30', '31_60', '61_90', '90_plus'.
    """
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1_30"
    if days_overdue <= 60:
        return "31_60"
    if days_overdue <= 90:
        return "61_90"
    return "90_plus"


def _error_text(response: httpx.Response) -> str:
    """Extract SAP's error message from a failed response, safely."""
    try:
        body = response.json()
        return str(body.get("error", {}).get("message", {}).get("value", ""))[:500]
    except Exception:  # noqa: BLE001 — error paths must not raise
        return response.text[:500]


def _redact_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Return query params safe to write to the audit table."""
    return {k: v for k, v in (params or {}).items() if k.startswith("$")}
