"""Old stock — goods a year or more in the warehouse (2026-10-17, the owner).

"Warn the Garmin manager and the Director about old products that are getting
closer to one year or exceeded." From the SAP push: every item in stock per
warehouse (``stock_items``) and each item's last purchase date (``products``,
OITM.LastPurDat). An item last bought a year ago or more has every unit on
hand at least that old — exact. One bought again since may still hold older
units this can't see (SAP's batch history isn't pushed) — the note says so.

  * a year or more — "ошган";
  * within 30 days of a year — "яқинлашмоқда";
  * with how many sold in the last 90 days (``item_sales_90d``): old and not
    selling is the stock to act on.

Every Monday 08:00 (agents/old-stock): the Director all warehouses, the Garmin
manager the Garmin warehouses (``BILLZ_SAP_WAREHOUSES``). /eski any time.
A PDF list, the text if the PDF can't be drawn.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from integrations.common.config import settings
from integrations.common.db import fetch_all
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local, to_local

AGENT = "old-stock"
log = setup_logging(AGENT)

COMMANDS = ("/eski", "/эски")
# Display samples ("Dummy Unit…", codes like 010-M2704-00) aren't stock for sale — left out (2026-10-17).
_DUMMY = re.compile(r"^\s*dummy|^\d{3}-M\d", re.I)
YEAR_DAYS = 365
SOON_DAYS = 30  # "getting closer": within a month of a year


@dataclass
class Row:
    code: str
    name: str
    whs: str
    whs_name: str
    qty: float
    value: float  # USD (SAP's local currency)
    last_buy: date
    days: int
    sold90: float


def _raw(row: dict[str, Any]) -> dict[str, Any]:
    return row["raw"] if isinstance(row["raw"], dict) else json.loads(row["raw"] or "{}")


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _day(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def classify(stock: list[dict[str, Any]], products: list[dict[str, Any]], sales90: list[dict[str, Any]],
             warehouses: list[dict[str, Any]], today: date, codes: frozenset[str] | None = None
             ) -> tuple[list[Row], list[Row]]:
    """(a year or more, within a month of a year) — rows of stock on hand, biggest value first."""
    last_buy = {str(p.get("ItemCode")): _day(p.get("LastPurDat")) for p in products}
    names = {str(w.get("WhsCode")): str(w.get("WhsName") or "") for w in warehouses}
    sold: dict[tuple[str, str], float] = {}
    for s in sales90:
        key = (str(s.get("ItemCode")), str(s.get("WhsCode")))
        sold[key] = sold.get(key, 0.0) + _num(s.get("Quantity"))
    old: list[Row] = []
    soon: list[Row] = []
    for r in stock:
        whs, code = str(r.get("WhsCode") or "").strip(), str(r.get("ItemCode") or "")
        if _DUMMY.search(str(r.get("ItemName") or "")) or _DUMMY.search(code):
            continue
        qty = _num(r.get("OnHand"))
        bought = last_buy.get(code)
        if qty <= 0 or bought is None or (codes is not None and whs not in codes):
            continue
        days = (today - bought).days
        row = Row(code, str(r.get("ItemName") or ""), whs, names.get(whs, ""), qty, _num(r.get("StockValue")),
                  bought, days, sold.get((code, whs), 0.0))
        if days >= YEAR_DAYS:
            old.append(row)
        elif days >= YEAR_DAYS - SOON_DAYS:
            soon.append(row)
    key = lambda x: (-x.value, -x.days)  # noqa: E731
    return sorted(old, key=key), sorted(soon, key=key)


def garmin_codes(warehouses: list[dict[str, Any]] | None = None) -> frozenset[str]:
    """Every Garmin warehouse: the shops' (BILLZ_SAP_WAREHOUSES) and every SAP warehouse named «Garmin»
    (19.10.2026: 09 «Garmin zahiralar», 10 Ambassador, 03 Bodomzor, 04 Abay, 06 faulty had been left out)."""
    from integrations.billz import sap_check

    shops = {c for group in sap_check.warehouse_map(settings.billz_sap_warehouses).values() for c in group}
    named = {str(w.get("WhsCode")) for w in warehouses or [] if "garmin" in str(w.get("WhsName") or "").lower()}
    return frozenset(shops | named)


async def garmin_codes_now() -> frozenset[str]:
    rows = await fetch_all("SELECT raw FROM v_sap_gateway_latest WHERE tool = 'warehouses'")
    return garmin_codes([_raw(r) for r in rows])


async def load(codes: frozenset[str] | None = None) -> tuple[list[Row], list[Row], Any]:
    async def kind(tool: str) -> list[dict[str, Any]]:
        return [_raw(r) for r in await fetch_all("SELECT raw FROM v_sap_gateway_latest WHERE tool = %s", (tool,))]

    stock = await kind("stock_items")
    rows = await fetch_all("SELECT max(captured_at) AS at FROM v_sap_gateway_latest WHERE tool = 'stock_items'")
    old, soon = classify(stock, await kind("products"), await kind("item_sales_90d"), await kind("warehouses"),
                         today_local(), codes)
    return old, soon, rows[0]["at"] if rows else None


def _usd(value: float) -> str:
    return f"${value:,.0f}"


def caption(title: str, old: list[Row], soon: list[Row]) -> str:
    idle = sum(1 for r in old if r.sold90 <= 0)
    lines = [f"⏳ <b>{title}</b>",
             f"Бир йил ва ундан кўп турган: <b>{len(old)}</b> қатор, {_usd(sum(r.value for r in old))}"
             + (f" — {idle} тасидан 90 кунда биттаям сотилмаган" if old else ""),
             f"Бир йилга яқинлашмоқда (30 кун ичида): <b>{len(soon)}</b> қатор, {_usd(sum(r.value for r in soon))}"]
    return "\n".join(lines)


def view(title: str, old: list[Row], soon: list[Row], as_of: Any) -> dict[str, Any]:
    def rows(items: list[Row]) -> list[dict[str, Any]]:
        return [{"code": r.code, "name": r.name, "whs": r.whs_name or r.whs, "qty": f"{r.qty:g}", "value": _usd(r.value),
                 "bought": r.last_buy.strftime("%d.%m.%Y"), "months": r.days // 30, "sold90": f"{r.sold90:g}",
                 "idle": r.sold90 <= 0} for r in items]

    return {"title": title, "date": today_local().strftime("%d.%m.%Y"),
            "as_of": to_local(as_of).strftime("%d.%m.%Y %H:%M") if as_of else "—",
            "old": rows(old), "soon": rows(soon),
            "old_value": _usd(sum(r.value for r in old)), "soon_value": _usd(sum(r.value for r in soon))}


def text_fallback(title: str, old: list[Row], soon: list[Row]) -> str:
    lines = [caption(title, old, soon), ""]
    for label, items in (("Бир йилдан ошган", old), ("Яқинлашмоқда", soon)):
        if items:
            lines.append(f"<b>{label}</b> (энг қимматидан):")
            lines += [f"• {r.name[:40]} — {r.whs_name or r.whs}: {r.qty:g} дона, {_usd(r.value)}, охирги харид "
                      f"{r.last_buy:%d.%m.%Y}" for r in items[:25]]
            if len(items) > 25:
                lines.append(f"… яна {len(items) - 25} та")
    return "\n".join(lines)


async def send(chat_id: int, codes: frozenset[str] | None, run_id: uuid.UUID) -> str:
    """The list to one chat: everything (the Director) or the given warehouses (the Garmin manager)."""
    from integrations.reports import render
    from integrations.telegram.bot import TelegramBot

    title = "Эски товарлар — Garmin омборлари" if codes is not None else "Эски товарлар — ҳамма омборлар"
    old, soon, as_of = await load(codes)
    async with TelegramBot(agent=AGENT, run_id=run_id,
                           bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
        if not old and not soon:
            await bot.send_message(f"⏳ <b>{title}</b>\nБир йилга яқинлашган ёки ошган товар йўқ.", chat_id=str(chat_id))
            return "old_stock_none"
        try:
            pdf = render.pdf("old_stock.html", **view(title, old, soon, as_of))
        except Exception as exc:  # noqa: BLE001 — the text instead
            log.warning("Old stock PDF failed: {}", type(exc).__name__)
            await bot.send_message(text_fallback(title, old, soon), chat_id=str(chat_id))
            return "old_stock_text"
        await bot.send_file(pdf, f"eski-tovarlar-{today_local():%Y-%m-%d}.pdf", str(chat_id), caption(title, old, soon))
    return "old_stock_sent"
