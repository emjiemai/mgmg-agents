"""Who didn't enter SAP — /kiritish (2026-11-03, the owner: «can we see who didn't enter SAP»).

What the systems show, person by person, for the last ``DAYS`` days:
  * **the shop** — Billz cheques that never reached SAP, reached it late, or with another amount, by the Billz
    seller (from the 08:00 Billz → SAP check, ``billz_sap_checks``; 03.11: one seller had 17 of 19 missing);
  * **SAP invoices entered late** — created in SAP after their own date, by salesperson and by the SAP user who typed
    them (names once the gateway sends ``UserName`` / ``SlpName``; until then «SAP фойдаланувчи №10»);
  * **invoices without a salesperson**, by the user who entered them.
Not visible yet: warehouse receipts / issues and spare parts used (the gateway pushes no inventory documents) —
said, never guessed. The Director sees everyone; the Garmin manager his team (the shop and Garmin сотув).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from integrations.common.db import fetch_all
from integrations.common.translit import latin_to_cyrillic
from integrations.telegram.bot import escape

COMMANDS = ("/kiritish", "/киритиш")
DAYS = 7


def _som(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " сўм"


def _close(a: str, b: str) -> bool:
    """Two name words alike enough (Ширина ≈ Ширин, Хажиев ≈ Хожиев)."""
    from difflib import SequenceMatcher

    return a == b or (min(len(a), len(b)) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.8)


def display_name(name: str, people: list[dict[str, Any]]) -> str:
    """A Billz / SAP name as the bot knows the person — two words alike, any order, Latin or Cyrillic — else
    the name in Cyrillic."""
    from integrations.org_bot.plan_facts import fold

    raw = " ".join(str(name or "").split())
    words = fold(raw).split()
    found = []
    for p in people:
        mine = fold(f"{p.get('full_name') or ''} {p.get('display_name') or ''}").split()
        if sum(1 for w in set(words) if any(_close(w, m) for m in mine)) >= min(2, len(set(words))) and words:
            found.append(p)
    if len(found) == 1:
        return found[0].get("full_name") or found[0].get("display_name") or raw
    return latin_to_cyrillic(raw) if raw else "(кўрсатилмаган)"


def shop_by_seller(checks: list[dict[str, Any]], people: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per seller: cheques missing in SAP (and their sum), entered late, another amount."""
    out: dict[str, dict[str, Any]] = {}
    for res in checks:
        for kind, items in (("missing", res.get("missing") or []), ("late", [x["cheque"] for x in res.get("late") or []]),
                            ("amount", [x["cheque"] for x in res.get("amount_diff") or []])):
            for c in items:
                who = display_name(c.get("seller") or "", people)
                s = out.setdefault(who, {"missing": 0, "missing_sum": 0.0, "late": 0, "amount": 0})
                s[kind] += 1
                if kind == "missing":
                    s["missing_sum"] += float(c.get("amount") or 0)
    return out


def sap_by_person(sales: list[dict[str, Any]], salespeople: dict[str, str], start: date) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """Per salesperson: invoices, entered late, the longest delay (days); per entering user: invoices without a salesperson."""
    from integrations.reconcile.core import to_date

    docs: dict[tuple[str, str], dict[str, Any]] = {}
    for r in sales:
        if str(r.get("CANCELED") or "N").upper() == "N":
            docs.setdefault((str(r.get("ObjType") or "13"), str(r.get("DocEntry"))), r)
    per: dict[str, dict[str, int]] = {}
    no_slp: dict[str, int] = {}
    for r in docs.values():
        day, made = to_date(r.get("DocDate")), to_date(r.get("CreateDate"))
        if day is None or max(day, made or day) < start:
            continue
        user = str(r.get("UserName") or "").strip() or f"SAP фойдаланувчи №{r.get('UserSign')}"
        slp = str(r.get("SlpCode"))
        if slp in ("-1", "", "None"):
            no_slp[user] = no_slp.get(user, 0) + 1
            continue
        name = str(r.get("SlpName") or "").strip() or salespeople.get(slp, f"сотувчи №{slp}")
        s = per.setdefault(name, {"docs": 0, "late": 0, "max_days": 0})
        s["docs"] += 1
        delay = (made - day).days if made and day else 0
        if delay >= 1:
            s["late"] += 1
            s["max_days"] = max(s["max_days"], delay)
    return per, no_slp


def report_text(shop: dict[str, dict[str, Any]], sap: dict[str, dict[str, int]], no_slp: dict[str, int],
                start: date, end: date) -> str:
    lines = [f"🧾 <b>SAP'га ким киритмади</b> · {start:%d.%m}–{end:%d.%m}", ""]
    lines.append("<b>Дўкон: Billz чеклари → SAP</b>")
    bad = {k: v for k, v in shop.items() if v["missing"] or v["late"] or v["amount"]}
    if not bad:
        lines.append("✅ Ҳамма чек SAP'га ўз вақтида ва тўғри суммада киритилган.")
    for who, s in sorted(bad.items(), key=lambda x: -x[1]["missing"]):
        parts = []
        if s["missing"]:
            parts.append(f"киритилмаган {s['missing']} та ({_som(s['missing_sum'])})")
        if s["late"]:
            parts.append(f"кеч киритилган {s['late']} та")
        if s["amount"]:
            parts.append(f"суммаси бошқа {s['amount']} та")
        lines.append(f"{'🔴' if s['missing'] else '🟡'} {escape(who)} — " + ", ".join(parts))
    lines += ["", "<b>SAP счёт-фактуралари: кеч киритилган</b> (санасидан кейинги кунларда)"]
    late = {k: v for k, v in sap.items() if v["late"]}
    if not late:
        lines.append("✅ Ҳаммаси ўз кунида киритилган.")
    for who, s in sorted(late.items(), key=lambda x: -x[1]["late"]):
        lines.append(f"🟡 {escape(who)} — {s['late']} / {s['docs']} та кеч, энг кўпи {s['max_days']} кун")
    if no_slp:
        lines += ["", "<b>Сотувчи кўрсатилмаган счётлар</b> (ким киритган)"]
        lines += [f"• {escape(u)} — {n} та" for u, n in sorted(no_slp.items(), key=lambda x: -x[1])]
    lines += ["", "<i>Кўринмайди: омбор кирим / чиқими ва ишлатилган эҳтиёт қисмлар — SAP шлюзи бу ҳужжатларни ҳали "
              "юбормайди. Billz сотувчилари — эрталабки Billz → SAP текширувидан.</i>"]
    return "\n".join(lines)


async def build(team_roles: tuple[str, ...] | None, today: date) -> str:
    """The report for the Director (team_roles None — everyone) or a manager (his team's people only)."""
    from integrations.reconcile.run import sap_tool

    start, end = today - timedelta(days=DAYS), today - timedelta(days=1)
    people = await fetch_all("SELECT * FROM employees WHERE status = 'active'")
    rows = await fetch_all("SELECT result FROM billz_sap_checks WHERE check_date BETWEEN %s AND %s", (start, end))
    checks = [r["result"] if isinstance(r["result"], dict) else json.loads(r["result"] or "{}") for r in rows]
    shop = shop_by_seller(checks, people)
    salespeople = {str(s.get("SlpCode")): str(s.get("SlpName") or "") for s in await sap_tool("sales_people")}
    sap_raw, no_slp = sap_by_person(await sap_tool("sales"), salespeople, start)
    sap: dict[str, dict[str, int]] = {}
    for who, v in sap_raw.items():  # SAP's «Умматова Ширин» as the bot's «Ширин Умматова»
        s = sap.setdefault(display_name(who, people), {"docs": 0, "late": 0, "max_days": 0})
        s["docs"] += v["docs"]
        s["late"] += v["late"]
        s["max_days"] = max(s["max_days"], v["max_days"])
    if team_roles is not None:
        team = [p for p in people if p.get("role") in team_roles]
        names = {display_name(p.get("full_name") or p.get("display_name") or "", team) for p in team}
        keep = lambda who: display_name(who, team) in names  # noqa: E731
        sap = {k: v for k, v in sap.items() if keep(k)}
        no_slp = {}
    return report_text(shop, sap, no_slp, start, end)
