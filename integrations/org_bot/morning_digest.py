"""The Boss's morning package, right after the brief picture (2026-11-19, the owner: «he is not seeing the brief, so we
need to use psychology, we need to prove that this system is already working»).

The picture lost «Ойлик мақсадлар» and «Топшириқлар» the same day. After it every active Director gets, each as its
own message, in this order:

  1. the report calendar — the /hisobotlar picture (the command left his menu), the month of yesterday;
  2. what was sold yesterday — every SAP invoice dated yesterday (the brief's «Кечаги сотув»): salesperson, customer,
     product × pieces and so'm; then the shops' Billz cheques by seller (shop sales are entered into SAP too, so the
     two lists can hold the same sale — said in the message);
  3. the daily reports of the last asked day IN FULL — each person's own words, the time, late / built from chat /
     files; who didn't send;
  4. what everyone did that day — per person: the report, task steps, daily duty cards, complaints closed, lead
     answers, explanations, questions to the work AI — and what the bot itself did (messages, AI checker decisions).

Each message ends with what he can ask for in words (his menu is PULSE only). A part that fails is logged and skipped —
never stops the others or the brief; the package goes once a day (``morning_digest_sent``). Logs keep counts only, and
nothing of this reaches IT (the order of 07.10.2026).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.common.money import NBSP, format_uzs, format_uzs_short, to_tiyin
from integrations.common.timeutil import now_local, parse_sap_date, to_local, today_local

AGENT = "morning-digest"
log = setup_logging(AGENT)

MAX_ITEM_LINES = 120   # product lines in the sales message before «+N»
MAX_DOC_LINES = 12     # one invoice's product lines before «+N»
WRAP = 900             # a report line longer than this is wrapped at a space (split_message keeps tags per line)
NO_SALESPERSON = frozenset({"", "-no sales employee-", "no sales employee", "-"})
# Task steps that are the person's own doing (or its result), as PULSE names them; the rest are the system's.
PERSON_EVENTS = ("accepted", "blocked", "unblocked", "submitted", "closed", "returned", "reschedule_asked")
LEAD_WORDS = {"done": "сотилди", "in_progress": "жараёнда", "dismissed": "сотилмади", "not_contacted": "боғланилмаган"}


# ------------------------------------------------------------------------------------------------------ helpers

def _raw(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value or "{}")


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _qty(value: float) -> str:
    q = abs(value)
    return f"{q:.0f}" if q == int(q) else f"{q:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def _som(value: float) -> str:
    """«5 490 000» — a line's exact so'm."""
    return format_uzs(to_tiyin(abs(value)), with_currency=False).replace(NBSP, " ")


def _total(value: float) -> str:
    """«14,76 млн сўм» — a head line's total."""
    return format_uzs_short(to_tiyin(value)).replace(NBSP, " ")


def day_label(day: date) -> str:
    from integrations.org_bot.task_tracker import WEEKDAYS_UZ

    return f"{day:%d.%m}, {WEEKDAYS_UZ[day.weekday()]}"


def _esc(text: Any) -> str:
    from integrations.telegram.bot import escape

    return escape(" ".join(str(text or "").split()))


def _price(qty: float, amount: float | None) -> str:
    """«4 490 000 сўм» for one piece; «2 × 9 805 000 = 19 610 000 сўм» for more; pieces only without a sum."""
    if amount is None:
        return f"{_qty(qty)} дона"
    if abs(qty) in (0, 1):
        return f"{_som(amount)} сўм"
    return f"{_qty(qty)} × {_som(amount / abs(qty))} = {_som(amount)} сўм"


def _wrap(text: str, width: int = WRAP) -> list[str]:
    """A report's lines, a very long one broken at a space — the message is split at line ends."""
    out: list[str] = []
    for line in str(text or "").splitlines():
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            cut = cut if cut > width // 2 else width
            out.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        out.append(line)
    return out


def _people_names(staff: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(e["id"]): e for e in staff}


# -------------------------------------------------------------------------------------------------------- sales

@dataclass
class Doc:
    credit: bool
    number: str
    customer: str
    total: float                                     # so'm, credit notes negative
    lines: list[tuple[str, float, float | None]] = field(default_factory=list)  # (product, pieces, so'm)


def sap_documents(rows: list[dict[str, Any]], day: date, people: list[dict[str, Any]]) -> dict[str, list[Doc]]:
    """SAP's pushed invoice lines dated ``day`` → documents per salesperson (as the bot knows the person), the biggest
    first. A document without a salesperson goes under who entered it."""
    from integrations.org_bot.sap_entry import display_name

    docs: dict[tuple[str, str], tuple[str, Doc]] = {}
    for r in rows:
        if parse_sap_date(str(r.get("DocDate") or "")) != day or str(r.get("CANCELED") or "N").upper() != "N":
            continue
        credit = str(r.get("ObjType") or "13") == "14"
        key = ("14" if credit else "13", str(r.get("DocEntry") or r.get("DocNum") or id(r)))
        if key not in docs:
            slp = " ".join(str(r.get("SlpName") or "").split())
            if slp.lower() in NO_SALESPERSON:
                user = " ".join(str(r.get("UserName") or "").split())
                seller = f"Сотувчи кўрсатилмаган · киритган: {display_name(user, people)}" if user else "Сотувчи кўрсатилмаган"
            else:
                seller = display_name(slp, people)
            total = _num(r.get("DocTotalSy")) or _num(r.get("DocTotal"))
            docs[key] = (seller, Doc(credit=credit, number=str(r.get("DocNum") or ""),
                                     customer=" ".join(str(r.get("CardName") or "").split()),
                                     total=-total if credit else total))
        amount = r.get("GTotalSC") if r.get("GTotalSC") not in (None, "") else r.get("TotalSumSy")
        docs[key][1].lines.append((" ".join(str(r.get("Dscription") or r.get("ItemCode") or "—").split()),
                                   _num(r.get("Quantity")), None if amount in (None, "") else _num(amount)))
    out: dict[str, list[Doc]] = defaultdict(list)
    for seller, doc in docs.values():
        out[seller].append(doc)
    for seller in out:
        out[seller].sort(key=lambda d: -d.total)
    return dict(sorted(out.items(), key=lambda kv: -sum(d.total for d in kv[1])))


@dataclass
class ShopLine:
    product: str
    qty: float
    amount: float


def billz_lines(rows: list[dict[str, Any]], people: list[dict[str, Any]]) -> dict[str, dict[str, list[ShopLine]]]:
    """Billz cheque lines → shop → seller → lines (returns as negative lines)."""
    from integrations.billz.sap_check import _line_amount
    from integrations.org_bot.sap_entry import display_name

    out: dict[str, dict[str, list[ShopLine]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        qty = r.get("net_sold_measurement_value")
        line = ShopLine(" ".join(str(r.get("product_name") or "—").split()),
                        _num(qty if qty is not None else r.get("sold_measurement_value")), _line_amount(r))
        if not line.amount and not line.qty:
            continue
        seller = " ".join(str(r.get("seller_full_name") or "").split())
        out[" ".join(str(r.get("shop_name") or "").split()) or "Дўкон"][
            display_name(seller, people) if seller else "Сотувчи кўрсатилмаган"].append(line)
    return {shop: dict(sellers) for shop, sellers in out.items()}


def sales_text(day: date, sap_rows: list[dict[str, Any]] | None, sap_as_of: datetime | None,
               shop_rows: list[dict[str, Any]] | None, people: list[dict[str, Any]], now: datetime,
               billz_on: bool = True) -> str:
    """The sales message: SAP invoices by salesperson, then the shops' cheques by seller."""
    lines = [f"💰 <b>Кечаги сотувлар — {day_label(day)}</b>"]
    budget = MAX_ITEM_LINES
    left = 0
    if sap_rows is None:
        lines += ["", "<b>SAP:</b> маълумот олиб бўлмади"]
    else:
        groups = sap_documents(sap_rows, day, people)
        docs = [d for g in groups.values() for d in g]
        if not docs:
            lines += ["", f"<b>SAP:</b> {day:%d.%m} санали ҳисоб-фактура йўқ"]
        else:
            invoices = [d for d in docs if not d.credit]
            credits = len(docs) - len(invoices)
            lines += ["", f"<b>SAP · {len(invoices)} та ҳисоб-фактура · {_total(sum(d.total for d in docs))}</b>"
                      + (f" · {credits} та қайтариш" if credits else "")]
            for seller, mine in groups.items():
                lines += ["", f"👤 <b>{_esc(seller)}</b> — {_total(sum(d.total for d in mine))}"
                          + (f" · {len(mine)} та ҳужжат" if len(mine) > 1 else "")]
                for d in mine:
                    who = _esc(d.customer) or "мижоз кўрсатилмаган"
                    lines.append(f"↩️ қайтариш · {who} — −{_som(d.total)} сўм" if d.credit
                                 else f"🧾 {who} — {_som(d.total)} сўм")
                    shown = d.lines[:min(MAX_DOC_LINES, max(budget, 0))]
                    for product, qty, amount in shown:
                        lines.append(f"   • {_esc(product)} — {_price(qty, amount)}")
                    budget -= len(shown)
                    if len(d.lines) > len(shown):
                        left += len(d.lines) - len(shown)
                        lines.append(f"   +яна {len(d.lines) - len(shown)} та товар")
        if sap_as_of is not None and to_local(sap_as_of).date() < now.date():
            lines.append(f"⚠️ SAP маълумоти {to_local(sap_as_of):%d.%m %H:%M} ҳолатига — кейин киритилгани бу ерда йўқ")
    if billz_on:
        if shop_rows is None:
            lines += ["", "<b>Дўкон кассаси (Billz):</b> маълумот олиб бўлмади"]
        else:
            shops = billz_lines(shop_rows, people)
            if not shops:
                lines += ["", "<b>Дўкон кассаси (Billz):</b> кеча сотув йўқ"]
            for shop, sellers in shops.items():
                all_lines = [x for g in sellers.values() for x in g]
                lines += ["", f"<b>Дўкон кассаси (Billz) · {_esc(shop)} · {_total(sum(x.amount for x in all_lines))}</b>",
                          "<i>дўкон чеклари SAP'га ҳам киритилади — юқоридаги рўйхатда ҳам бўлиши мумкин</i>"]
                for seller, mine in sorted(sellers.items(), key=lambda kv: -sum(x.amount for x in kv[1])):
                    lines += ["", f"👤 <b>{_esc(seller)}</b> — {_total(sum(x.amount for x in mine))}"]
                    shown = mine[:max(budget, 0)]
                    for x in shown:
                        lines.append(f"   {'↩️' if x.amount < 0 or x.qty < 0 else '•'} {_esc(x.product)} — "
                                     f"{_price(x.qty, x.amount)}")
                    budget -= len(shown)
                    if len(mine) > len(shown):
                        left += len(mine) - len(shown)
                        lines.append(f"   +яна {len(mine) - len(shown)} та қатор")
    lines += ["", "💬 <i>Тўлиқ рўйхатни сўранг: «кечаги сотувларни Excel'да юбор»</i>" if left
              else "💬 <i>Сўранг: «кечаги сотувларни Excel'да юбор», «Garmin қолдиғи»</i>"]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------ reports

def reports_text(rows: list[dict[str, Any]]) -> str | None:
    """The last asked day's reports in full: who wrote what and when; late, built from chat, files; who didn't send.
    None when nobody was asked."""
    from integrations.org_bot.roles import ROLE_LABELS

    if not rows:
        return None
    day = rows[0]["report_date"]

    def order(r: dict[str, Any]) -> tuple[str, str]:
        return ROLE_LABELS.get(r.get("role") or "", r.get("role") or ""), str(r.get("display_name") or "")

    rows = sorted(rows, key=order)
    sent = [r for r in rows if r.get("status") == "submitted"]
    late = [r for r in rows if r.get("status") == "asked" and r.get("late_content")]
    missed = [r for r in rows if r.get("status") == "asked" and not r.get("late_content")]
    excused = [r for r in rows if r.get("status") == "excused"]
    asked = len(rows) - len(excused)
    built = sum(1 for r in sent if r.get("auto_built"))
    head = f"{asked} кишидан {len(sent)} таси юборди" + (f" ({built} таси чатдан тузилди)" if built else "")
    if late:
        head += f" · {len(late)} та кечиккан"
    if missed:
        head += f" · {len(missed)} таси юбормади"
    if excused:
        head += f" · {len(excused)} таси сабабли"
    lines = [f"📝 <b>Кечаги ҳисоботлар — {day_label(day)}</b>", head]
    for i, r in enumerate(sent + late, 1):
        role = ROLE_LABELS.get(r.get("role") or "", r.get("role") or "")
        meta = [_esc(role)] if role else []
        if r in late:
            meta.append(f"кечиккан · {to_local(r['late_sent_at']):%d.%m %H:%M}" if r.get("late_sent_at") else "кечиккан")
            meta.append("рад этилди" if r.get("late_decision") == "declined" else "қарор кутилмоқда")
            body = r.get("late_content")
        else:
            if r.get("submitted_at"):
                meta.append(f"{to_local(r['submitted_at']):%H:%M}" if to_local(r["submitted_at"]).date() == day
                            else f"{to_local(r['submitted_at']):%d.%m %H:%M}")
            if r.get("late_decision") == "approved":
                meta.append("кечиккан, қабул қилинди")
            if r.get("auto_built"):
                meta.append("22:00 да чатдан тузилди")
            body = r.get("content")
        if int(r.get("media_count") or 0):
            meta.append(f"📎 {int(r['media_count'])} та файл")
        lines += ["", f"<b>{i}. {_esc(r.get('display_name'))}</b> · " + " · ".join(meta)]
        text = str(body or "").replace("[чатдан тузилди]", "").strip()
        lines += [_esc(x) if x.strip() else "" for x in _wrap(text)] if text else ["<i>матни йўқ</i>"]
    if missed:
        lines += ["", f"🔴 <b>Юбормаганлар ({len(missed)}):</b> "
                  + ", ".join(f"{_esc(r.get('display_name'))} ({_esc(ROLE_LABELS.get(r.get('role') or '', r.get('role') or ''))})"
                              for r in missed)]
    example = next((r for r in sent if int(r.get("media_count") or 0)), sent[0] if sent else None)
    if example is not None:
        what = "ҳисоботидаги файлларни юбор" if int(example.get("media_count") or 0) else "кечаги ҳисоботини кўрсат"
        lines += ["", f"💬 <i>Сўранг: «{_esc(example.get('display_name'))}нинг {what}»</i>"]
    return "\n".join(line for line in lines)


# ------------------------------------------------------------------------------------------------------ actions

@dataclass
class DayActions:
    """One day's recorded actions — rows straight from the bot's own tables."""

    events: list[dict[str, Any]] = field(default_factory=list)       # task steps: at, event, code, pid
    duties: list[dict[str, Any]] = field(default_factory=list)       # pid, done, not_done, missed, useless
    resolved: list[dict[str, Any]] = field(default_factory=list)     # complaints closed: resolved_by
    complaints_new: int = 0
    leads: list[dict[str, Any]] = field(default_factory=list)        # telegram_user_id, status, n
    explained: list[dict[str, Any]] = field(default_factory=list)    # pid, kind
    ai_questions: list[dict[str, Any]] = field(default_factory=list)  # telegram_user_id, n
    messages: dict[str, int] = field(default_factory=dict)           # sent, people
    checker: dict[str, int] = field(default_factory=dict)            # decision → n
    tasks_given: int = 0


def _events_part(events: list[dict[str, Any]]) -> str:
    from integrations.pulse.data import TASK_EVENT_LABELS

    mine = [e for e in events if e.get("event") in PERSON_EVENTS]
    if not mine:
        return ""
    if len(mine) <= 4:
        return "📌 " + ", ".join(f"{_esc(e.get('code') or '')} {TASK_EVENT_LABELS.get(e['event'], e['event']).lower()}"
                                 f" {to_local(e['at']):%H:%M}".strip() for e in mine)
    counts: dict[str, int] = defaultdict(int)
    for e in mine:
        counts[TASK_EVENT_LABELS.get(e["event"], e["event"]).lower()] += 1
    return f"📌 топшириқлар бўйича {len(mine)} та қадам (" + ", ".join(f"{k} {v}" for k, v in counts.items()) + ")"


def actions_text(day: date, staff: list[dict[str, Any]], reports: list[dict[str, Any]], acts: DayActions) -> str:
    """Who did what that day, as the bot recorded it: the bot's own work first, then each person."""
    from integrations.org_bot import greetings, names
    from integrations.org_bot.roles import ROLE_LABELS

    report_of = {str(r.get("pid")): r for r in reports if r.get("report_date") == day}
    events_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in sorted(acts.events, key=lambda e: e["at"]):
        events_of[str(e.get("pid"))].append(e)
    duties_of = {str(d["pid"]): d for d in acts.duties}
    explained_of = {str(x["pid"]) for x in acts.explained}
    ai_of = {int(x["telegram_user_id"]): int(x["n"]) for x in acts.ai_questions}
    leads_of: dict[int, dict[str, int]] = defaultdict(dict)
    for x in acts.leads:
        leads_of[int(x["telegram_user_id"])][str(x["status"])] = int(x["n"])
    resolved_of: dict[str, int] = defaultdict(int)
    for c in acts.resolved:
        who = greetings.match(str(c.get("resolved_by") or ""), staff)
        if who is not None:
            resolved_of[str(who["id"])] += 1

    submitted = sum(1 for r in report_of.values() if r.get("status") == "submitted")
    asked = sum(1 for r in report_of.values() if r.get("status") != "excused")
    steps = sum(1 for e in acts.events if e.get("event") in PERSON_EVENTS)
    bot = []
    if acts.messages.get("sent"):
        bot.append(f"{acts.messages.get('people', 0)} кишига {acts.messages['sent']} та хабар юборди")
    if asked:
        bot.append(f"{submitted} / {asked} ҳисобот қабул қилди")
    if acts.tasks_given:
        bot.append(f"{acts.tasks_given} та янги топшириқ тарқатди")
    if steps:
        bot.append(f"топшириқлар бўйича {steps} та қадамни ёзди")
    decided = acts.checker.get("approve", 0) + acts.checker.get("reject", 0)
    if decided or acts.checker.get("unsure"):
        bot.append(f"AI текширувчи {decided} та қарор чиқарди ({acts.checker.get('approve', 0)} ✅, "
                   f"{acts.checker.get('reject', 0)} ❌)" + (f", {acts.checker['unsure']} тасини сизга қолдирди"
                                                           if acts.checker.get("unsure") else ""))
    if acts.complaints_new or acts.resolved:
        bot.append(f"шикоятлар: {acts.complaints_new} та келди, {len(acts.resolved)} та ҳал бўлди")
    lines = [f"👥 <b>Кеча ким нима қилди — {day_label(day)}</b>"]
    if bot:
        lines.append("🤖 Бот: " + " · ".join(bot))

    done: list[tuple[str, str, str]] = []  # (role, name, line)
    idle: list[str] = []
    for e in staff:
        pid, tg = str(e["id"]), int(e.get("telegram_user_id") or 0)
        parts: list[str] = []
        r = report_of.get(pid)
        if r is not None:
            if r.get("status") == "excused":
                parts.append("⬜ сабабли кун")
            elif r.get("status") == "submitted" and r.get("auto_built"):
                parts.append("📝 ҳисобот чатдан тузилди")
            elif r.get("status") == "submitted" and r.get("submitted_at"):
                parts.append(f"📝 ҳисобот {to_local(r['submitted_at']):%H:%M}")
            elif r.get("late_content"):
                parts.append("📝 кечиккан ҳисобот")
            else:
                parts.append("📝 ҳисобот йўқ")
        if events := _events_part(events_of.get(pid, [])):
            parts.append(events)
        if d := duties_of.get(pid):
            card = [f"{int(d[k])} {mark}" for k, mark in (("done", "✅"), ("not_done", "⛔"), ("useless", "👎")) if int(d.get(k) or 0)]
            if int(d.get("missed") or 0):
                card.append(f"{int(d['missed'])} жавобсиз")
            if card:
                parts.append("📋 кунлик карталар: " + " ".join(card))
        if resolved_of.get(pid):
            parts.append(f"✅ {resolved_of[pid]} та шикоятни ҳал қилди")
        if mine := leads_of.get(tg):
            parts.append("🎯 лидлар: " + ", ".join(f"{LEAD_WORDS.get(k, k)} {v}" for k, v in mine.items()))
        if pid in explained_of:
            parts.append("✍️ тушунтириш ёзди")
        if ai_of.get(tg):
            parts.append(f"💬 AI'га {ai_of[tg]} та савол")
        name = names.person_name(e)
        role = ROLE_LABELS.get(e.get("role") or "", e.get("role") or "")
        active = [p for p in parts if p not in ("📝 ҳисобот йўқ", "⬜ сабабли кун")]
        if active:
            done.append((role, name, f"<b>{_esc(name)}</b> · {_esc(role)}\n   " + " · ".join(parts)))
        elif r is not None and r.get("status") == "excused":
            continue
        else:
            idle.append(name)
    for _role, _name, line in sorted(done):
        lines += ["", line]
    if idle:
        lines += ["", f"⚪ <b>Бот орқали ҳеч нарса қилмаганлар ({len(idle)}):</b> " + ", ".join(_esc(n) for n in sorted(idle))]
    lines += ["", "💬 <i>Менюда фақат PULSE қолди — қолганини сўзда сўранг: «KPI жадвали», «ҳисоботлар календари», "
                  "«вазифалар реестри», «лидлар статистикаси», «…га ойлик мақсад қўй», «…нинг кунини сабабли қил».</i>"]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------ reading

async def report_rows(today: date) -> list[dict[str, Any]]:
    """Every report row of the last day before today anyone was asked — with the text (the brief's reports day)."""
    from integrations.common.db import fetch_all

    return await fetch_all(
        """SELECT r.report_date, r.status, r.content, r.submitted_at, r.auto_built, r.media_count, r.late_content,
                  r.late_sent_at, r.late_decision, r.employee_id::text AS pid, e.role,
                  COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name
           FROM daily_reports r JOIN employees e ON e.id = r.employee_id
           WHERE r.report_date = (SELECT max(report_date) FROM daily_reports WHERE report_date < %s)
             AND e.status = 'active'""", (today,))


async def sap_sales(day: date) -> tuple[list[dict[str, Any]], datetime | None]:
    """SAP's latest pushed invoice lines (every pushed day) and when they came."""
    from integrations.common.db import fetch_all

    rows = await fetch_all("SELECT raw, captured_at FROM v_sap_gateway_latest WHERE tool = 'sales_lines'")
    return [_raw(r["raw"]) for r in rows], max((r["captured_at"] for r in rows if r.get("captured_at")), default=None)


async def shop_sales(day: date, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every Billz cheque line of ``day`` in every shop."""
    from integrations.billz.client import BillzClient

    async with BillzClient(agent=AGENT, run_id=run_id) as client:
        shops = [s for s in await client.shops() if s.get("id") and not s.get("deleted_at")]
        if not shops:
            return []
        return await client.positions_kept(day, today_local(), [str(s["id"]) for s in shops])


async def day_actions(day: date) -> DayActions:
    """``day``'s actions from the bot's own tables (Tashkent dates)."""
    from integrations.common.db import fetch_all

    local = "AT TIME ZONE 'Asia/Tashkent')::date = %s"
    (events, duties, resolved, new, leads, explained, ai, messages, checker, given) = await asyncio.gather(
        fetch_all(f"""SELECT e.at, e.event, t.code, t.assigned_employee_id::text AS pid
                      FROM task_events e JOIN tasks t ON t.id = e.task_id
                      WHERE t.assigned_employee_id IS NOT NULL AND (e.at {local}""", (day,)),
        fetch_all(f"""SELECT employee_id::text AS pid,
                             count(*) FILTER (WHERE status = 'done' AND (answered_at {local}) AS done,
                             count(*) FILTER (WHERE status = 'not_done' AND (answered_at {local}) AS not_done,
                             count(*) FILTER (WHERE status = 'missed' AND period_end = %s) AS missed,
                             count(*) FILTER (WHERE useless AND (useless_at {local}) AS useless
                      FROM duty_tasks
                      WHERE (answered_at {local} OR (status = 'missed' AND period_end = %s) OR (useless_at {local}
                      GROUP BY employee_id""", (day, day, day, day, day, day, day)),
        fetch_all(f"SELECT resolved_by FROM client_feedback WHERE kind = 'complaint' AND (resolved_at {local}", (day,)),
        fetch_all(f"SELECT count(*) AS n FROM client_feedback WHERE kind = 'complaint' AND (created_at {local}", (day,)),
        fetch_all(f"""SELECT telegram_user_id, status, count(*) AS n FROM lead_checkins
                      WHERE status IS NOT NULL AND (answered_at {local} GROUP BY telegram_user_id, status""", (day,)),
        fetch_all(f"SELECT employee_id::text AS pid, kind FROM explanations WHERE (answered_at {local}", (day,)),
        fetch_all(f"""SELECT telegram_user_id, count(*) AS n FROM ai_chat_turns
                      WHERE role = 'employee' AND (created_at {local} GROUP BY telegram_user_id""", (day,)),
        fetch_all("SELECT coalesce(sum(sent), 0) AS sent, count(*) FILTER (WHERE sent > 0) AS people "
                  "FROM message_budget WHERE day = %s", (day,)),
        fetch_all(f"""SELECT decision, count(*) AS n FROM ai_decisions
                      WHERE undone_at IS NULL AND (created_at {local} GROUP BY decision""", (day,)),
        fetch_all(f"SELECT count(*) AS n FROM task_events WHERE event = 'created' AND (at {local}", (day,)),
    )
    return DayActions(
        events=events, duties=duties, resolved=resolved, complaints_new=int((new or [{}])[0].get("n") or 0),
        leads=leads, explained=explained, ai_questions=ai,
        messages={"sent": int((messages or [{}])[0].get("sent") or 0), "people": int((messages or [{}])[0].get("people") or 0)},
        checker={str(r["decision"]): int(r["n"]) for r in checker}, tasks_given=int((given or [{}])[0].get("n") or 0))


# ------------------------------------------------------------------------------------------------------ sending

async def _sent_today(day: date) -> bool:
    from integrations.common.db import fetch_one

    row = await fetch_one(
        """SELECT 1 AS x FROM agent_actions WHERE agent = %s AND action = 'morning_digest_sent' AND status = 'success'
           AND (occurred_at AT TIME ZONE 'Asia/Tashkent')::date = %s LIMIT 1""", (AGENT, day))
    return row is not None


async def build(run_id: uuid.UUID) -> tuple[tuple[bytes | None, str] | None, list[tuple[str, str]], dict[str, Any]]:
    """The package: the calendar (picture, caption), the texts in order, and the counts for the log."""
    from integrations.org_bot import report_calendar, store
    from integrations.org_bot.roles import DIRECTOR_ROLE

    today, now = today_local(), now_local()
    yesterday = today - timedelta(days=1)
    people = await store.list_active_employees()
    staff = [e for e in people if e.get("role") != DIRECTOR_ROLE]
    counts: dict[str, Any] = {"parts": [], "failed": []}
    calendar = None
    try:
        png, view = await report_calendar.picture(day=yesterday)
        calendar = (png, report_calendar.caption(view) if png is not None else report_calendar.text_fallback(view))
        counts["parts"].append("calendar")
    except Exception as exc:  # noqa: BLE001 — the other parts still go
        log.error("Digest calendar failed: {}", type(exc).__name__)
        counts["failed"].append("calendar")
    texts: list[tuple[str, str]] = []
    try:
        sap_rows, as_of = await sap_sales(yesterday)
    except Exception as exc:  # noqa: BLE001
        log.error("Digest SAP sales not read: {}", type(exc).__name__)
        sap_rows, as_of = None, None
    shop_rows: list[dict[str, Any]] | None = None
    if settings.billz_configured:
        try:
            shop_rows = await shop_sales(yesterday, run_id)
        except Exception as exc:  # noqa: BLE001
            log.error("Digest Billz sales not read: {}", type(exc).__name__)
    try:
        texts.append(("sales", sales_text(yesterday, sap_rows, as_of, shop_rows, people, now, settings.billz_configured)))
        counts["sap_docs"] = sum(len(v) for v in sap_documents(sap_rows or [], yesterday, people).values())
        counts["shop_lines"] = len(shop_rows or [])
    except Exception as exc:  # noqa: BLE001
        log.error("Digest sales failed: {}", type(exc).__name__)
        counts["failed"].append("sales")
    rows: list[dict[str, Any]] = []
    try:
        rows = await report_rows(today)
        text = reports_text(rows)
        if text:
            texts.append(("reports", text))
        counts["reports"] = sum(1 for r in rows if r.get("status") == "submitted")
    except Exception as exc:  # noqa: BLE001
        log.error("Digest reports failed: {}", type(exc).__name__)
        counts["failed"].append("reports")
    try:
        day = rows[0]["report_date"] if rows and (today - rows[0]["report_date"]).days <= 3 else yesterday
        texts.append(("actions", actions_text(day, staff, rows, await day_actions(day))))
        counts["people"] = len(staff)
    except Exception as exc:  # noqa: BLE001
        log.error("Digest actions failed: {}", type(exc).__name__)
        counts["failed"].append("actions")
    counts["parts"] += [part for part, _ in texts]
    return calendar, texts, counts


async def send(run_id: uuid.UUID, force: bool = False) -> dict[str, Any]:
    """The package to every active Director, once a day (after the brief picture). Counts only."""
    from integrations.common.db import log_action
    from integrations.org_bot import store
    from integrations.org_bot.roles import DIRECTOR_ROLE
    from integrations.telegram.bot import TelegramBot, TelegramError

    if not settings.morning_digest_enabled or settings.bots_frozen:
        return {"sent": 0}
    today = today_local()
    if not force and not settings.dry_run and await _sent_today(today):
        log.info("Morning digest already sent today")
        return {"sent": 0}
    calendar, texts, counts = await build(run_id)
    if settings.dry_run:
        import tempfile
        from pathlib import Path

        if calendar is not None and calendar[0] is not None:
            preview = Path(tempfile.gettempdir()) / "digest-calendar.png"
            preview.write_bytes(calendar[0])
            log.info("[dry run] calendar written to {}", preview)
        for _part, text in texts:
            print(text, "\n" + "-" * 60)
        return {"sent": 0, **counts}
    directors = await store.active_employees_by_role(DIRECTOR_ROLE)
    sent = 0
    async with TelegramBot(agent=AGENT, run_id=run_id,
                           bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
        for boss in directors:
            chat = str(boss["telegram_user_id"])
            try:
                if calendar is not None:
                    if calendar[0] is not None:
                        await bot.send_file(calendar[0], "hisobotlar.png", chat, calendar[1], photo=True)
                    else:
                        await bot.send_message(calendar[1], chat_id=chat)
                for _part, text in texts:
                    await bot.send_message(text, chat_id=chat)
                sent += 1
            except TelegramError as exc:
                log.error("Morning digest not delivered to one Director: {}", type(exc).__name__)
    await log_action(agent=AGENT, action="morning_digest_sent", target_system="telegram",
                     status="success" if sent else "failure", run_id=run_id, mode="notify",
                     payload={**counts, "directors": sent})
    return {"sent": sent, **counts}
