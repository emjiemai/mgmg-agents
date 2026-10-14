"""Agent — the Garmin manager's morning brief (2026-10-14, the owner).

"Just like the Director's, but the Garmin section": the same designed
picture (``brief.html``), with only Garmin's figures, to everyone holding the
``garmin_manager`` role, at 08:00 after the Billz → SAP check.

Five figures:
  1. shop sales yesterday — BILLZ (so'm, cheques);
  2. Garmin's sales in SAP yesterday — invoices minus credit notes with a line
     in the Garmin warehouses (``BILLZ_SAP_WAREHOUSES``), in so'm;
  3. Garmin stock — SAP's stock value of those warehouses (USD);
  4. open Garmin complaint tickets (and how many came yesterday);
  5. Garmin AI bot leads yesterday (how many want to buy now).
Blocks: the shops, the Billz → SAP check (cheques not in SAP), Garmin sales
people who didn't report, the open complaints.

A source that can't be read says so on its row; the brief still goes. Sent
once a day (``agent_actions`` 'garmin_brief_sent'); ``--force`` sends again.

Run:
    python agents/garmin-brief/agent.py             # send
    python agents/garmin-brief/agent.py --dry-run   # write the picture to a temp file, send nothing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.billz import sales as billz_sales  # noqa: E402
from integrations.billz import sap_check  # noqa: E402
from integrations.common.config import settings  # noqa: E402
from integrations.common.db import close_pool, fetch_all, fetch_one, log_action  # noqa: E402
from integrations.common.logging_setup import setup_logging  # noqa: E402
from integrations.common.money import format_money  # noqa: E402
from integrations.common.timeutil import fmt_date, now_local, to_local, today_local  # noqa: E402
from integrations.onec import cash as onec_cash  # noqa: E402
from integrations.org_bot import feedback  # noqa: E402
from integrations.org_bot import store as org_store  # noqa: E402
from integrations.telegram.bot import TelegramBot, TelegramError  # noqa: E402

AGENT = "garmin-brief"
ROLE = "garmin_manager"
SALES_ROLE = "garmin_sotuv"
MAX_LINES = 5
NBSP = " "
log = setup_logging(AGENT)


@dataclass
class GarminData:
    day: date
    shop_sales: billz_sales.DaySales | None = None
    shop_failed: bool = False
    sap_amount: int | None = None   # so'm, invoices − credit notes, Garmin warehouses, yesterday
    sap_docs: int = 0
    sap_unknown: int = 0            # documents without a so'm total
    sap_as_of: datetime | None = None
    stock_usd: float | None = None
    complaints_open: list[dict[str, Any]] = field(default_factory=list)
    complaints_new: int = 0
    leads: int = 0
    leads_now: int = 0
    check: dict[str, Any] | None = None  # yesterday's Billz → SAP result (billz_sap_checks)
    reports: list[dict[str, Any]] | None = None
    errors: list[str] = field(default_factory=list)


def warehouses() -> frozenset[str]:
    return frozenset(c for group in sap_check.warehouse_map(settings.billz_sap_warehouses).values() for c in group)


async def _latest(tool: str) -> tuple[list[dict[str, Any]], datetime | None]:
    rows = await fetch_all("SELECT raw, captured_at FROM v_sap_gateway_latest WHERE tool = %s", (tool,))
    raws = [r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"] or "{}") for r in rows]
    return raws, max((r["captured_at"] for r in rows), default=None)


def sap_sales(sales: list[dict[str, Any]], lines: list[dict[str, Any]], day: date,
              codes: frozenset[str]) -> tuple[int, int, int]:
    """(so'm total, documents, documents without a so'm total) for one day in the Garmin warehouses."""
    docs = [d for d in sap_check.docs_from_sap(sales, lines, codes) if d.day == day]
    return sum(d.amount for d in docs if d.amount_known), len(docs), sum(1 for d in docs if not d.amount_known)


def stock_value(rows: list[dict[str, Any]], codes: frozenset[str]) -> float | None:
    mine = [r for r in rows if str(r.get("WhsCode") or "").strip() in codes]
    if not mine:
        return None
    return sum(float(r.get("StockValue") or 0) for r in mine)


async def collect(today: date | None = None) -> GarminData:
    today = today or today_local()
    data = GarminData(day=today - timedelta(days=1))
    codes = warehouses()

    if settings.billz_configured:
        try:
            data.shop_sales = await billz_sales.load_day(data.day, run_id=None, agent=AGENT)
        except Exception as exc:  # noqa: BLE001 — a source that fails says so on its row
            data.shop_failed = True
            data.errors.append(f"billz: {type(exc).__name__}")
    try:
        sales, as_of = await _latest("sales")
        lines, _ = await _latest("sales_lines")
        if sales:
            data.sap_amount, data.sap_docs, data.sap_unknown = sap_sales(sales, lines, data.day, codes)
            data.sap_as_of = as_of
        stock, _ = await _latest("stock_value")
        data.stock_usd = stock_value(stock, codes)
    except Exception as exc:  # noqa: BLE001
        data.errors.append(f"sap: {type(exc).__name__}")
    try:
        rows = [r for r in await org_store.recent_client_feedback(days=60) if r.get("place") == "garmin"]
        data.complaints_open = [r for r in rows if r.get("ticket_status") == "open"]
        data.complaints_new = sum(1 for r in rows if to_local(r["created_at"]).date() == data.day)
    except Exception as exc:  # noqa: BLE001
        data.errors.append(f"complaints: {type(exc).__name__}")
    try:
        row = await fetch_one(
            """SELECT count(*) AS n, count(*) FILTER (WHERE urgency = 'now') AS now_n FROM garmin_leads
               WHERE (created_at AT TIME ZONE 'Asia/Tashkent')::date = %s""",
            (data.day,),
        )
        data.leads, data.leads_now = int(row["n"]), int(row["now_n"])
    except Exception as exc:  # noqa: BLE001
        data.errors.append(f"leads: {type(exc).__name__}")
    try:
        row = await fetch_one("SELECT result FROM billz_sap_checks WHERE check_date = %s", (data.day,))
        if row and row.get("result"):
            data.check = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    except Exception as exc:  # noqa: BLE001
        data.errors.append(f"billz_sap: {type(exc).__name__}")
    if settings.daily_reports_enabled:
        try:
            data.reports = [r for r in await org_store.report_results_before(today) if r["role"] == SALES_ROLE]
        except Exception as exc:  # noqa: BLE001
            data.errors.append(f"reports: {type(exc).__name__}")
    return data


def _split(value: str) -> tuple[str, str]:
    number, _, unit = value.replace(NBSP, " ").rpartition(" ")
    return (number, unit) if number else (value, "")


def _row(label: str, icon: str) -> dict[str, Any]:
    return {"label": label, "icon": icon, "tone": "plain", "detail": None, "number": "", "unit": "",
            "lower": False, "sub": None, "sub_dir": None, "sub_alert": False}


def _block(title: str, icon: str, day: str | None, summary: str, tone: str, rows: list[dict[str, Any]],
           empty: str | None = None) -> dict[str, Any]:
    return {"title": title, "icon": icon, "day": day, "summary": summary, "tone": tone,
            "rows": rows[:MAX_LINES], "more": max(len(rows) - MAX_LINES, 0), "empty": empty}


def _hours_open(created: datetime, now: datetime) -> int:
    """Whole hours since a complaint came, on Tashkent wall-clock time (``now`` aware or not)."""
    return max(0, int((now.replace(tzinfo=None) - to_local(created).replace(tzinfo=None)).total_seconds() // 3600))


def view(data: GarminData, now: datetime | None = None) -> dict[str, Any]:
    """The brief template's values — the Garmin section only."""
    from integrations.org_bot.task_tracker import MONTHS_UZ, WEEKDAYS_UZ

    now = now or now_local()
    today = now.date()
    when = f"{WEEKDAYS_UZ[today.weekday()].capitalize()}, {today.day} {MONTHS_UZ[today.month - 1]} {today.year} · {now:%H:%M}"

    shop = _row("Дўкон савдоси (Billz)", "shop")
    if data.shop_failed:
        shop.update(tone="muted", number="Billz'дан олинмади")
    elif data.shop_sales is None:
        shop.update(tone="muted", number="уланмаган")
    elif not data.shop_sales.orders:
        shop.update(tone="quiet", number="0", detail="кеча сотув йўқ")
    else:
        number, unit = _split(onec_cash.som(data.shop_sales.net_sales))
        shop.update(number=number, unit=unit, detail=f"{data.shop_sales.orders} та чек")

    sap = _row("SAP'даги сотув", "sales")
    if data.sap_amount is None:
        sap.update(tone="muted", number="SAP'дан келмаган")
    elif not data.sap_docs:
        sap.update(tone="quiet", number="0", detail="Garmin омборларидан ҳужжат йўқ")
    else:
        number, unit = _split(onec_cash.som(data.sap_amount))
        sap.update(number=number, unit=unit, detail=f"{data.sap_docs} та ҳужжат · Garmin омборлари")
        if data.sap_unknown:
            sap.update(sub=f"{data.sap_unknown} тасида сўмдаги сумма йўқ", sub_alert=True)
    if data.sap_as_of is not None and to_local(data.sap_as_of).date() < today:
        sap.update(sub=f"SAP {to_local(data.sap_as_of):%d.%m %H:%M} ҳолатида", sub_alert=True)

    stock = _row("Garmin захираси", "stock")
    if data.stock_usd is None:
        stock.update(tone="muted", number="маълумот йўқ")
    else:
        number, unit = _split(format_money(round(data.stock_usd * 100), "USD"))
        stock.update(number=number, unit=unit, detail="SAP, Garmin омборлари")

    complaints = _row("Очиқ шикоятлар", "alert")
    complaints.update(number=str(len(data.complaints_open)), detail=f"кеча келгани: {data.complaints_new} та")
    if data.complaints_open:
        complaints["tone"] = "alert"
    else:
        complaints["tone"] = "quiet"

    leads = _row("Garmin бот лидлари", "people")
    leads.update(number=str(data.leads), detail=f"кеча · ҳозир олмоқчи: {data.leads_now} та",
                 tone="plain" if data.leads else "quiet")

    blocks: list[dict[str, Any]] = []
    if data.shop_sales is not None and len([s for s in data.shop_sales.shops if s.orders or s.net_sales]) > 1:
        rows = [{"who": s.name, "what": f"{onec_cash.som(s.net_sales).replace(NBSP, ' ')} · {s.orders} та чек",
                 "alert": False} for s in data.shop_sales.shops if s.orders or s.net_sales]
        blocks.append(_block("Дўконлар", "shop", fmt_date(data.day), "", "plain", rows))
    if data.check is not None:
        missing = data.check.get("missing") or []
        wrong = data.check.get("amount_diff") or []
        if missing or wrong:
            rows = [{"who": f"Чек {c.get('number')}", "what": f"SAP'га киритилмаган · {c.get('shop') or ''}".strip(" ·"),
                     "alert": True} for c in missing]
            rows += [{"who": f"Чек {p['cheque'].get('number')}", "what": "SAP'да суммаси бошқа", "alert": True} for p in wrong]
            blocks.append(_block("Billz → SAP", "report", fmt_date(data.day),
                                 f"{len(missing)} та киритилмаган · {len(wrong)} та сумма фарқи", "alert", rows))
        else:
            blocks.append(_block("Billz → SAP", "ok", fmt_date(data.day), "ҳамма чек SAP'да", "ok", []))
    if data.reports is not None and data.reports:
        missed = [r for r in data.reports if r["status"] != "submitted"]
        if missed:
            blocks.append(_block("Ҳисобот юбормаганлар", "report", fmt_date(data.reports[0]["report_date"]),
                                 f"{len(missed)} / {len(data.reports)}", "alert",
                                 [{"who": r["display_name"], "what": "Garmin сотув", "alert": False} for r in missed]))
        else:
            blocks.append(_block("Кунлик ҳисоботлар", "report", fmt_date(data.reports[0]["report_date"]),
                                 f"ҳаммаси юборди · {len(data.reports)}", "ok", []))
    if data.complaints_open:
        rows = [{"who": r.get("code") or "—",
                 "what": f"{feedback.where(r.get('place'), r.get('branch'))} · "
                         f"{_hours_open(r['created_at'], now)} соат",
                 "alert": True} for r in data.complaints_open]
        blocks.append(_block("Очиқ шикоятлар", "alert", None, f"{len(data.complaints_open)} та", "alert", rows))
    notice = "Баъзи маълумотлар олинмади: " + ", ".join(data.errors) if data.errors else None
    return {"title": "Garmin кунлик ҳисоботи", "date": fmt_date(today), "when": when,
            "foot": "Дўконлар — Billz · сотув ва захира — SAP (Garmin омборлари) · шикоятлар — QR саҳифа · лидлар — "
                    "Garmin бот. Батафсил: OPS Manager'дан сўранг.",
            "figures": [shop, sap, stock, complaints, leads], "blocks": blocks, "lower_bound": False, "notice": notice}


def caption(data: GarminData) -> str:
    return f"Garmin кунлик ҳисоботи — {fmt_date(data.day + timedelta(days=1))}"


def text(view_data: dict[str, Any]) -> str:
    """The text fallback when the picture can't be drawn."""
    lines = [f"<b>{view_data['title']} — {view_data['date']}</b>", ""]
    for f in view_data["figures"]:
        value = f"{f['number']} {f['unit']}".strip()
        lines.append(f"• {f['label']}: {value}" + (f" ({f['detail']})" if f.get("detail") else ""))
    for b in view_data["blocks"]:
        lines += ["", f"<b>{b['title']}</b>" + (f": {b['summary']}" if b.get("summary") else "")]
        lines += [f"  – {r['who']}: {r['what']}" for r in b["rows"]]
    if view_data.get("notice"):
        lines += ["", view_data["notice"]]
    return "\n".join(lines)


async def _sent_today(day: date) -> bool:
    row = await fetch_one(
        """SELECT 1 AS x FROM agent_actions WHERE agent = %s AND action = 'garmin_brief_sent' AND status = 'success'
           AND (occurred_at AT TIME ZONE 'Asia/Tashkent')::date = %s LIMIT 1""",
        (AGENT, day),
    )
    return row is not None


async def run(dry_run: bool = False, force: bool = False) -> int:
    if dry_run:
        settings.dry_run = True
    today = today_local()
    people = await org_store.active_employees_by_role(ROLE)
    if not people and not settings.dry_run:
        log.info("Nobody holds the Garmin manager role — no Garmin brief")
        return 0
    if not force and not settings.dry_run and await _sent_today(today):
        log.info("Garmin brief already sent today")
        return 0
    data = await collect(today)
    values = view(data)
    picture: bytes | None = None
    try:
        from integrations.reports import render as reports

        picture = reports.image("brief.html", **values)
    except Exception as exc:  # noqa: BLE001 — never lose the brief over its layout
        log.error("Garmin brief image failed, sending text: {}", type(exc).__name__)
    if settings.dry_run:
        print(text(values))
        if picture is not None:
            import tempfile

            preview = Path(tempfile.gettempdir()) / "garmin-brief-preview.png"
            preview.write_bytes(picture)
            log.info("[dry run] Garmin brief written to {}", preview)
        return 0
    if settings.bots_frozen:
        log.info("Bots frozen — Garmin brief not sent")
        return 0
    run_id = uuid.uuid4()
    sent = 0
    async with TelegramBot(agent=AGENT, run_id=run_id,
                           bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
        for person in people:
            try:
                if picture is not None:
                    await bot.send_file(picture, f"garmin-{today:%Y-%m-%d}.png", str(person["telegram_user_id"]),
                                        caption(data), photo=True)
                else:
                    await bot.send_message(text(values), chat_id=str(person["telegram_user_id"]))
                sent += 1
            except TelegramError as exc:
                log.error("Garmin brief not delivered to one person: {}", exc)
    await log_action(agent=AGENT, action="garmin_brief_sent", target_system="telegram",
                     status="success" if sent else "failure", run_id=run_id, mode="notify",
                     payload={"people": sent, "errors": len(data.errors)})
    return 0 if sent else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="The Garmin manager's morning brief.")
    parser.add_argument("--dry-run", action="store_true", help="print and draw it, send nothing")
    parser.add_argument("--force", action="store_true", help="send even if already sent today")
    args = parser.parse_args()

    async def _main() -> int:
        try:
            return await run(args.dry_run, args.force)
        finally:
            await close_pool()

    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
