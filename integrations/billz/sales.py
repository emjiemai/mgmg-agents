"""Shop sales from BILLZ: yesterday for the brief, 30 days for the Director's questions.

Pure functions over the report rows (tested offline in scripts/selfcheck.py)
plus ``load_*`` that read them. Amounts arrive in whole so'm; "сотув" here is
BILLZ's ``net_gross_sales`` — revenue after discounts and returns — so it
matches what the shop actually kept, not the till's gross.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from integrations.common.money import format_uzs_short, to_tiyin
from integrations.common.timeutil import fmt_date
from integrations.telegram.bot import escape


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class ShopDay:
    name: str
    net_sales: float = 0.0
    gross_sales: float = 0.0
    orders: int = 0
    returns: int = 0


@dataclass
class DaySales:
    day: date
    shops: list[ShopDay] = field(default_factory=list)

    @property
    def net_sales(self) -> float:
        return sum(s.net_sales for s in self.shops)

    @property
    def orders(self) -> int:
        return sum(s.orders for s in self.shops)


def _row_day(row: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(row.get("date") or "")[:10])
    except ValueError:
        return None


def day_sales(rows: list[dict[str, Any]], day: date) -> DaySales:
    """One day's sales per shop, biggest first (``general-report-table`` rows)."""
    shops: dict[str, ShopDay] = {}
    for row in rows:
        if _row_day(row) != day:
            continue
        name = str(row.get("shop_name") or row.get("shop_id") or "—")
        s = shops.setdefault(name, ShopDay(name=name))
        s.net_sales += _num(row.get("net_gross_sales"))
        s.gross_sales += _num(row.get("gross_sales"))
        s.orders += int(_num(row.get("orders_count")))
        s.returns += int(_num(row.get("returns_count")))
    return DaySales(day=day, shops=sorted(shops.values(), key=lambda s: -s.net_sales))


def _som(amount: float) -> str:
    return format_uzs_short(to_tiyin(amount))


def render_day(sales: DaySales, max_lines: int = 5) -> str:
    """The brief's block: total, then each shop."""
    label = fmt_date(sales.day)
    if not sales.shops or (sales.net_sales == 0 and sales.orders == 0):
        return f"🛍 <b>Дўконлар (Billz), {label}:</b> сотув бўлмаган\n"
    lines = [f"🛍 <b>Дўконлар (Billz), {label}:</b> {_som(sales.net_sales)} · {sales.orders} та чек"]
    # Warehouses and service points appear in the report with nothing sold — not news.
    selling = [s for s in sales.shops if s.net_sales or s.orders or s.returns]
    for s in selling[:max_lines]:
        returns = f", {s.returns} та қайтариш" if s.returns else ""
        lines.append(f"   • {escape(s.name)} — {_som(s.net_sales)} ({s.orders} та чек{returns})")
    if len(selling) > max_lines:
        lines.append(f"   <i>+яна {len(selling) - max_lines} та дўкон</i>")
    return "\n".join(lines) + "\n"


def describe(shop_rows: list[dict[str, Any]], seller_rows: list[dict[str, Any]],
             product_rows: list[dict[str, Any]], start: date, end: date) -> str:
    """Plain-text data for OPS Manager Bot's answers (amounts in so'm)."""
    lines = [
        f"BILLZ retail shop sales {start} to {end} (amounts in UZS so'm; 'net' = after discounts and returns; "
        "dates in Tashkent time). This is the shops' till system — separate from SAP."
    ]
    totals: dict[str, list[float]] = {}
    for row in shop_rows:
        t = totals.setdefault(str(row.get("shop_name") or "—"), [0.0, 0.0, 0.0])
        t[0] += _num(row.get("net_gross_sales"))
        t[1] += _num(row.get("orders_count"))
        t[2] += _num(row.get("gross_profit"))
    lines.append("PER SHOP, whole period: " + ("; ".join(
        f"{name}: net {round(v[0]):,} so'm, {int(v[1])} sales, gross profit {round(v[2]):,}"
        for name, v in sorted(totals.items(), key=lambda kv: -kv[1][0])) or "no sales"))
    lines.append("PER SHOP PER DAY (net so'm / sales count):")
    for row in sorted(shop_rows, key=lambda r: (str(r.get("date")), str(r.get("shop_name")))):
        if _num(row.get("net_gross_sales")) or _num(row.get("orders_count")):
            lines.append(f"- {str(row.get('date'))[:10]} {row.get('shop_name')}: "
                         f"{round(_num(row.get('net_gross_sales'))):,} / {int(_num(row.get('orders_count')))}")
    if seller_rows:
        lines.append("PER SELLER, whole period (net so'm, sales, average cheque):")
        for row in sorted(seller_rows, key=lambda r: -_num(r.get("net_gross_sales"))):
            lines.append(f"- {row.get('seller_name') or '—'}: {round(_num(row.get('net_gross_sales'))):,}, "
                         f"{int(_num(row.get('orders_count')))} sales, avg {round(_num(row.get('average_cheque'))):,}")
    if product_rows:
        lines.append("TOP PRODUCTS, whole period (net so'm, units sold):")
        for row in sorted(product_rows, key=lambda r: -_num(r.get("net_sales")))[:25]:
            lines.append(f"- {row.get('product_name') or '—'}: {round(_num(row.get('net_sales'))):,}, "
                         f"{_num(row.get('net_sold_measurement_value')):g} pcs")
    return "\n".join(lines)


async def load_day(day: date, run_id: uuid.UUID | str | None, agent: str) -> DaySales:
    from integrations.billz.client import BillzClient

    async with BillzClient(agent=agent, run_id=run_id) as client:
        return day_sales(await client.shop_days(day, day), day)


async def load_period(end: date, days: int, run_id: uuid.UUID | str | None, agent: str) -> str:
    from integrations.billz.client import BillzClient

    start = end - timedelta(days=days - 1)
    async with BillzClient(agent=agent, run_id=run_id) as client:
        shop_rows = await client.shop_days(start, end)
        seller_rows = await client.sellers(start, end)
        shop_ids = [str(s["id"]) for s in await client.shops() if s.get("id") and not s.get("deleted_at")]
        product_rows = await client.products(start, end, shop_ids) if shop_ids else []
    return describe(shop_rows, seller_rows, product_rows, start, end)
