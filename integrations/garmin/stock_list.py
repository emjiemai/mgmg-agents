"""Garmin stock for ordering — every item, how many left, how fast it sells (2026-10-27, Murod aka).

«Барча товарлар остаткаси керак, фақат Garmin'га тегишли SAP'даги маълумот … заказ уришдан олдин остаткага қараб
заказ уришим керак, шунда қайсидир кўп ёки кам бўлиб қолмайди.» /eski stays (old, unsold goods); this is the whole
Garmin stock, by quantity, for ordering:

  * every item on hand in a Garmin warehouse, and every item SOLD there in the last 90 days even if none is left
    (sold out is exactly what to order);
  * per Garmin warehouse, the total, sold in 90 days (SAP item_sales_90d), per month, how many months it lasts,
    on order in SAP, SAP's Min / Max level;
  * a mark: **тугаган** (none left, it sells) → **кам** (under a month, or under SAP's Min) → **етарли** (1–4
    months) → **кўп** (over 4 months) → **сотилмаяпти** (on hand, nothing sold in 90 days).

/qoldiq (the Garmin manager, the Director) sends a short summary and the .xlsx; asked in words, the analyst's
garmin_stock_list tool gives the same and attaches the file. Quantity only — no money. Display dummies left out.
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from integrations.common.config import settings
from integrations.common.db import fetch_all
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import to_local, today_local

AGENT = "garmin-stock"
log = setup_logging(AGENT)

COMMANDS = ("/qoldiq", "/қолдиқ", "/ostatka")
LOW_MONTHS = 1.0
HIGH_MONTHS = 4.0
STATUS_ORDER = ("тугаган", "кам", "етарли", "кўп", "сотилмаяпти")


@dataclass
class Item:
    code: str
    name: str
    group: str = ""
    per_wh: dict[str, float] = field(default_factory=dict)
    sold90: float = 0.0
    on_order: float = 0.0
    min_level: float = 0.0
    max_level: float = 0.0

    @property
    def total(self) -> float:
        return sum(self.per_wh.values())

    @property
    def per_month(self) -> float:
        return self.sold90 / 3

    @property
    def months(self) -> float | None:
        """How many months what's left lasts at the last 90 days' pace; None when nothing sold."""
        return self.total / self.per_month if self.per_month > 0 else None

    @property
    def status(self) -> str:
        if self.sold90 <= 0:
            return "сотилмаяпти"
        if self.total <= 0:
            return "тугаган"
        if (self.months or 0) < LOW_MONTHS or (self.min_level > 0 and self.total < self.min_level):
            return "кам"
        return "етарли" if (self.months or 0) <= HIGH_MONTHS else "кўп"


FAULTY = ("faulty", "брак", "brak", "nosoz", "носоз", "defect")  # broken goods aren't stock to sell (27.10)


def sellable(codes: frozenset[str], warehouses: list[dict[str, Any]]) -> frozenset[str]:
    """The Garmin warehouses without the faulty-goods ones («Faulty Product Garmin», 06)."""
    names = {str(w.get("WhsCode") or "").strip(): str(w.get("WhsName") or "").lower() for w in warehouses}
    return frozenset(c for c in codes if not any(word in names.get(c, "") for word in FAULTY))


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def build(stock: list[dict[str, Any]], products: list[dict[str, Any]], sales90: list[dict[str, Any]],
          codes: frozenset[str]) -> list[Item]:
    """Every Garmin item on hand or sold in 90 days, most urgent first (sold out, low …), then by what sells most."""
    from integrations.org_bot.old_stock import _DUMMY  # noqa: PLC2701 — the one definition of a display sample

    def real(code: str, name: str) -> bool:
        return not (_DUMMY.search(name) or _DUMMY.search(code))

    items: dict[str, Item] = {}
    for r in stock:
        whs, code, name = str(r.get("WhsCode") or "").strip(), str(r.get("ItemCode") or ""), str(r.get("ItemName") or "")
        qty = _num(r.get("OnHand"))
        if whs in codes and qty > 0 and real(code, name):
            item = items.setdefault(code, Item(code, name))
            item.per_wh[whs] = item.per_wh.get(whs, 0.0) + qty
    info = {str(p.get("ItemCode")): p for p in products}
    for s in sales90:
        whs, code = str(s.get("WhsCode") or "").strip(), str(s.get("ItemCode") or "")
        name = str(info.get(code, {}).get("ItemName") or "")
        if whs in codes and real(code, name):
            items.setdefault(code, Item(code, name)).sold90 += _num(s.get("Quantity"))
    for code, item in items.items():
        p = info.get(code, {})
        item.name = item.name or str(p.get("ItemName") or code)
        item.group = str(p.get("ItmsGrpNam") or "")
        item.on_order, item.min_level, item.max_level = _num(p.get("OnOrder")), _num(p.get("MinLevel")), _num(p.get("MaxLevel"))
    out = [i for i in items.values() if i.total > 0 or i.sold90 > 0]
    return sorted(out, key=lambda i: (STATUS_ORDER.index(i.status), -i.sold90, -i.total, i.name))


def _q(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") if value == int(value) else f"{value:,.1f}".replace(",", " ")


def summary(items: list[Item], as_of: Any = None, limit: int = 8) -> str:
    """The chat message: totals, then what to order first (sold out, low)."""
    from integrations.telegram.bot import escape

    counts = {s: sum(1 for i in items if i.status == s) for s in STATUS_ORDER}
    pieces = sum(i.total for i in items)
    when = f" · SAP {to_local(as_of):%d.%m %H:%M}" if as_of else ""
    lines = [f"📦 <b>Garmin қолдиғи — буюртма учун</b>{when}",
             f"Жами: <b>{_q(pieces)} дона</b>, {sum(1 for i in items if i.total > 0)} хил товар",
             "Тугаган {тугаган} · кам {кам} · етарли {етарли} · кўп {кўп} · сотилмаяпти {сотилмаяпти}".format(**counts)]
    urgent = [i for i in items if i.status in ("тугаган", "кам")][:limit]
    if urgent:
        lines.append("\n<b>Биринчи буюртма қилиш керак:</b>")
        for i in urgent:
            left = "қолмаган" if i.total <= 0 else f"{_q(i.total)} дона қолди"
            order = f", буюртмада {_q(i.on_order)}" if i.on_order else ""
            lines.append(f"• {escape(i.name[:60])} — {left}, 90 кунда {_q(i.sold90)} сотилган{order}")
    lines.append("\nТўлиқ рўйхат (ҳар бир омбор, неча ойга етади) — Excel файлда.")
    return "\n".join(lines)


def workbook_bytes(items: list[Item], warehouses: dict[str, str], as_of: Any = None) -> bytes:
    """The whole list as .xlsx: one row per item, a column per Garmin warehouse, filters on, header frozen."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    used = [c for c in warehouses if any(c in i.per_wh for i in items)]
    wb = Workbook()
    ws = wb.active
    ws.title = "Қолдиқ"
    ws.append([f"Garmin қолдиғи — буюртма учун · {today_local():%d.%m.%Y}"
               + (f" · SAP {to_local(as_of):%d.%m.%Y %H:%M} ҳолатида" if as_of else "")])
    ws["A1"].font = Font(bold=True, size=13)
    head = ["Ҳолат", "Товар", "Код", "Гуруҳ", *[warehouses[c] or c for c in used], "Жами қолдиқ", "90 кунда сотилди",
            "Ойига ўртача", "Неча ойга етади", "Буюртмада (SAP)", "Min", "Max"]
    ws.append(head)
    for cell in ws[2]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    fills = {"тугаган": "F8D7DA", "кам": "FCE8C8", "етарли": "E2F0E6", "кўп": "DDE7F5", "сотилмаяпти": "ECECEC"}
    for i in items:
        months = round(i.months, 1) if i.months is not None else None
        ws.append([i.status, i.name, i.code, i.group, *[i.per_wh.get(c, 0) for c in used], i.total, i.sold90,
                   round(i.per_month, 1), months, i.on_order, i.min_level or None, i.max_level or None])
        ws.cell(row=ws.max_row, column=1).fill = PatternFill("solid", fgColor=fills[i.status])
    ws.freeze_panes = "C3"
    ws.auto_filter.ref = f"A2:{ws.cell(row=2, column=len(head)).column_letter}{ws.max_row}"
    widths = [12, 46, 16, 16, *[11] * len(used), 11, 11, 10, 11, 11, 7, 7]
    for n, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=2, column=n).column_letter].width = w
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def file_name() -> str:
    return f"garmin-qoldiq-{today_local():%Y-%m-%d}.xlsx"


async def load() -> tuple[list[Item], dict[str, str], Any]:
    """(items, Garmin warehouse code → name, when SAP's stock arrived) from the latest push."""
    from integrations.org_bot import old_stock

    async def kind(tool: str) -> list[dict[str, Any]]:
        rows = await fetch_all("SELECT raw FROM v_sap_gateway_latest WHERE tool = %s", (tool,))
        return [r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"] or "{}") for r in rows]

    whs = await kind("warehouses")
    codes = sellable(old_stock.garmin_codes(whs), whs)
    names = {str(w.get("WhsCode") or "").strip(): str(w.get("WhsName") or "") for w in whs}
    at = await fetch_all("SELECT max(captured_at) AS at FROM v_sap_gateway_latest WHERE tool = 'stock_items'")
    items = build(await kind("stock_items"), await kind("products"), await kind("item_sales_90d"), codes)
    return items, {c: names.get(c, c) for c in sorted(codes)}, at[0]["at"] if at else None


async def send(chat_id: int, run_id: uuid.UUID) -> str:
    """/qoldiq: the summary and the .xlsx to one chat."""
    from integrations.telegram.bot import TelegramBot

    items, warehouses, as_of = await load()
    async with TelegramBot(agent=AGENT, run_id=run_id,
                           bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
        if not items:
            await bot.send_message("📦 Garmin омборларида SAP'дан қолдиқ маълумоти келмаган.", chat_id=str(chat_id))
            return "garmin_stock_empty"
        await bot.send_message(summary(items, as_of), chat_id=str(chat_id))
        await bot.send_file(workbook_bytes(items, warehouses, as_of), file_name(), str(chat_id))
    return "garmin_stock_sent"

