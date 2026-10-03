"""Dashboard numbers from the rows the SAP gateway pushes — and how far to trust them.

Every gateway tool is pulled with a row limit (``PUSH_CAP``, set in
``scripts/sap-gateway-push/push-ar-aging.ps1``). When a push returns exactly
that many rows there were probably more, so a total built from it is only a
lower bound: the figure is marked ``capped`` and shown as "камида" (at
least). A figure is never presented as complete when it can't be.

Field names are SAP Business One's own table columns (``DocDate``,
``DocTotal``, ``OnHand``...) — the same convention the invoice push uses and
was verified with. A row without the needed columns makes the figure
``unknown_format`` rather than a guessed number.

Pure functions only, so the rules are tested offline (scripts/selfcheck.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from integrations.common.money import to_tiyin
from integrations.common.timeutil import parse_sap_date

# Rows per tool per push — must match the script's -Body '{"limit":N}' values.
PUSH_CAP: dict[str, int] = {
    "invoices": 100,
    "orders": 100,
    "inventory": 100,
    "payments": 100,
    "customers": 100,
    "warehouses": 100,
    "products": 20,
}


@dataclass
class Figure:
    """One dashboard number with its provenance.

    Attributes:
        status: "ok", "no_data" (nothing pushed), "unknown_format" (rows lack
            the needed columns) or "stale" (last push is too old).
        totals: Minor units per currency code.
        count: How many records make up the figure.
        capped: The push hit its row limit, so this is a lower bound.
        as_of: The snapshot day the rows came from.
    """

    status: str = "no_data"
    totals: dict[str, int] = field(default_factory=dict)
    count: int = 0
    capped: bool = False
    as_of: date | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _pick(row: dict[str, Any], *names: str) -> Any:
    """The first of ``names`` present in the row (None if none is)."""
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _is_cancelled(row: dict[str, Any]) -> bool:
    return str(_pick(row, "CANCELED", "Canceled") or "N").upper() == "Y"


def is_capped(tool: str, rows_received: int | None) -> bool:
    """Whether a push of ``rows_received`` rows hit the tool's limit."""
    cap = PUSH_CAP.get(tool)
    return bool(cap and rows_received is not None and rows_received >= cap)


def push_capped(tool: str, payload: dict[str, Any] | None) -> bool:
    """Whether the latest push (its audit payload) was cut off at the gateway's limit.

    A push from a complete gateway tool (2026-10-03, docs/sap-gateway-tools.md)
    is marked ``complete`` and is never a lower bound, however many rows it has.
    """
    if not payload or payload.get("complete"):
        return False
    received = payload.get("rows_received")
    return is_capped(tool, int(received) if received is not None else None)


def documents_on(
    rows: list[dict[str, Any]], day: date, currency: str, *, tool: str, as_of: date | None, complete: bool = False
) -> Figure:
    """Total of the documents (orders, incoming payments) dated ``day``.

    Args:
        rows: Raw gateway rows for one snapshot.
        day: The document date to total.
        currency: Currency the amounts are in (SAP's local currency).
        tool: Gateway tool name, for the cap check.
        as_of: Snapshot day.
        complete: The push carried every row (read from SAP's database).

    Returns:
        The figure; ``unknown_format`` if no row has a date and a total.
    """
    if not rows:
        return Figure(status="no_data", as_of=as_of)
    capped = not complete and is_capped(tool, len(rows))
    readable = [r for r in rows if _pick(r, "DocDate") is not None and _pick(r, "DocTotal") is not None]
    if not readable:
        return Figure(status="unknown_format", as_of=as_of, capped=capped)

    total, count = 0, 0
    for row in readable:
        if _is_cancelled(row) or parse_sap_date(str(_pick(row, "DocDate"))) != day:
            continue
        total += to_tiyin(_pick(row, "DocTotal"))
        count += 1
    return Figure(
        status="ok",
        totals={currency: total} if count else {},
        count=count,
        capped=capped,
        as_of=as_of,
    )


def invoices_on(rows: list[dict[str, Any]], day: date, currency: str, *, as_of: date | None) -> Figure:
    """Sales on ``day`` from a complete ``sales`` push: A/R invoices less credit notes.

    Sales are invoices (SAP OINV), not orders (2026-10-03: on 02.10 the shops
    sold through invoices while no order was written). Cancelled documents
    and their cancellations (``CANCELED`` "Y"/"C") are left out; a credit
    note (``ObjType`` 14) is subtracted. ``DocTotal`` is SAP's local currency
    (USD here) even for a document written in so'm.

    Returns:
        The figure (``count`` = invoices); ``unknown_format`` if no row has a date and a total.
    """
    if not rows:
        return Figure(status="no_data", as_of=as_of)
    readable = [r for r in rows if _pick(r, "DocDate") is not None and _pick(r, "DocTotal") is not None]
    if not readable:
        return Figure(status="unknown_format", as_of=as_of)
    total, count = 0, 0
    for row in readable:
        if str(_pick(row, "CANCELED", "Canceled") or "N").upper() != "N":
            continue
        if parse_sap_date(str(_pick(row, "DocDate"))) != day:
            continue
        amount = to_tiyin(_pick(row, "DocTotal"))
        if str(row.get("ObjType")) == "14":
            total -= amount
        else:
            total += amount
            count += 1
    return Figure(status="ok", totals={currency: total} if (count or total) else {}, count=count, as_of=as_of)


def inventory_value(
    rows: list[dict[str, Any]], currency: str, *, as_of: date | None, complete: bool = False
) -> Figure:
    """Stock value: SAP's own ``StockValue`` per row, else ``OnHand × AvgPrice``.

    Args:
        complete: The push carried every row (read from SAP's database), so
            however many rows it has, the total is not a lower bound.

    Returns:
        The figure; ``unknown_format`` if rows carry neither form.
    """
    if not rows:
        return Figure(status="no_data", as_of=as_of)
    capped = not complete and is_capped("inventory", len(rows))
    total, count = 0, 0
    for row in rows:
        value = _pick(row, "StockValue")
        if value is None:
            on_hand, price = _pick(row, "OnHand"), _pick(row, "AvgPrice")
            if on_hand is None or price is None:
                continue
            try:
                value = float(on_hand) * float(price)
            except (TypeError, ValueError):
                continue
        total += to_tiyin(value)
        count += 1
    if not count or not total:
        # Stock worth exactly nothing while rows exist is a feed problem (a
        # column missing or empty, 2026-10-03), never a real "$0.00".
        return Figure(status="unknown_format", as_of=as_of, capped=capped)
    return Figure(status="ok", totals={currency: total}, count=count, capped=capped, as_of=as_of)


def change(today: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    """Per-currency difference, only for currencies present both days."""
    return {cur: today[cur] - previous[cur] for cur in today if cur in previous}
