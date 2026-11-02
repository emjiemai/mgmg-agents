"""Week 4 of automatic delegation — everyone connected, and the Boss's acceptance (2026-11-02, T-0055: «Барча
ходимларни агентга улаш … 30.11.2026 гача»).

  * **Who is connected** (``status``): the register's people in the bot or not (the technicians register with the
    «Техник» role), the bot's people with no duty in the register (a head adds their rows), and who answered their
    cards on the last working days.
  * **Acceptance** (``acceptance``): the ЭМЖИЕМ МИЯ document's condition for stage 1 — «ходимларнинг ≥ 90 % 5 иш куни
    кетма-кет чек-инга жавоб берди; ҳамма вазифа ботда». A card day counts when at least 90 % of the people who got
    the 08:45 card answered at least one line that day; the streak is the run of such days up to the last one.
  * Shown on the Director's and the admin's /vazifalar and in the Friday review; the Garmin manager's /vazifalar
    shows his team's day. Names and counts only — no targets or sums.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from integrations.common.db import fetch_all
from integrations.telegram.bot import escape

ANSWER_SHARE = 0.9
STREAK_DAYS = 5
NO_DUTIES_ROLES = ("operatsion_direktor",)  # the Owner / the Boss gets no tasks from the register


def streak(days: list[tuple[date, float]], share: float = ANSWER_SHARE) -> int:
    """How many of the latest card days in a row reached ``share`` (days newest first)."""
    n = 0
    for _day, s in days:
        if s < share:
            break
        n += 1
    return n


async def answer_shares(today: date, last: int = 10) -> list[tuple[date, float, int, int]]:
    """(day, share, answered, got a card) for the latest card days, newest first."""
    rows = await fetch_all(
        """SELECT c.day, count(DISTINCT c.employee_id) AS got,
                  count(DISTINCT c.employee_id) FILTER (WHERE EXISTS (
                      SELECT 1 FROM duty_tasks t WHERE t.employee_id = c.employee_id AND t.answered_at IS NOT NULL
                        AND (t.answered_at AT TIME ZONE 'Asia/Tashkent')::date = c.day)) AS answered
           FROM duty_cards c JOIN employees e ON e.id = c.employee_id AND e.status = 'active'
           WHERE c.kind = 'morning' AND c.day <= %s GROUP BY c.day ORDER BY c.day DESC LIMIT %s""", (today, last))
    return [(r["day"], r["answered"] / r["got"] if r["got"] else 0.0, int(r["answered"]), int(r["got"])) for r in rows]


async def acceptance(today: date) -> dict[str, Any]:
    shares = await answer_shares(today)
    run = streak([(d, s) for d, s, _a, _g in shares])
    return {"streak": run, "met": run >= STREAK_DAYS, "last": shares[0] if shares else None}


async def status(today: date) -> dict[str, Any]:
    register = await fetch_all("SELECT person, bool_or(employee_id IS NOT NULL) AS in_bot FROM duties WHERE active GROUP BY person")
    without = await fetch_all(
        """SELECT coalesce(nullif(btrim(e.full_name), ''), e.display_name) AS person FROM employees e
           WHERE e.status = 'active' AND e.role <> ALL(%s)
             AND NOT EXISTS (SELECT 1 FROM duties d WHERE d.employee_id = e.id AND d.active) ORDER BY 1""",
        (list(NO_DUTIES_ROLES),))
    return {"register": len(register), "in_bot": sum(1 for r in register if r["in_bot"]),
            "not_in_bot": sorted(r["person"] for r in register if not r["in_bot"]),
            "no_duties": [r["person"] for r in without], "acceptance": await acceptance(today)}


def status_text(s: dict[str, Any]) -> str:
    """The «connected» block of /vazifalar and the Friday review."""
    lines = [f"<b>Агентга уланиш</b> — реестрдаги {s['register']} кишидан {s['in_bot']} таси ботда"]
    if s["not_in_bot"]:
        lines.append("• Ботда йўқ (OPS Manager ботига ёзиб рўйхатдан ўтсин): " + ", ".join(escape(n) for n in s["not_in_bot"]))
    if s["no_duties"]:
        lines.append("• Ботда бор, реестрда вазифаси йўқ: " + ", ".join(escape(n) for n in s["no_duties"]))
    a = s["acceptance"]
    if a["last"] is None:
        lines.append(f"• Қабул шарти (≥ 90 % ходим {STREAK_DAYS} иш куни кетма-кет жавоб берди): кунлик карталар ҳали юборилмаган")
    else:
        day, share, answered, got = a["last"]
        lines.append(f"• Қабул шарти (≥ 90 % ходим {STREAK_DAYS} иш куни кетма-кет жавоб берди): {'✅ бажарилди' if a['met'] else '⏳'} "
                     f"{a['streak']}/{STREAK_DAYS} кун · охирги кун {day:%d.%m}: {answered}/{got} ({share:.0%})")
    return "\n".join(lines)


async def team_view(manager: dict[str, Any], today: date) -> str:
    """The Garmin manager's /vazifalar: his own duties and his team's day (who got what, how much is done)."""
    from integrations.org_bot import duties
    from integrations.org_bot.report_calendar import TEAM_OF

    own = await duties.own_view(manager)
    roles = list(TEAM_OF.get(manager.get("role") or "", ()))
    rows = await fetch_all(
        """SELECT coalesce(nullif(btrim(e.full_name), ''), e.display_name) AS person, count(*) AS total,
                  count(*) FILTER (WHERE t.status = 'done') AS done, count(*) FILTER (WHERE t.status IN ('not_done', 'missed')) AS no,
                  count(*) FILTER (WHERE t.useless) AS useless
           FROM duty_tasks t JOIN employees e ON e.id = t.employee_id
           WHERE e.status = 'active' AND e.role = ANY(%s) AND t.period_start <= %s AND t.period_end >= %s
           GROUP BY 1 ORDER BY 1""", (roles, today, today))
    lines = [own, "", f"👥 <b>Жамоа · {today:%d.%m}</b> (✅ / жами)"]
    if not rows:
        lines.append("Бугун жамоага реестрдан вазифа берилмаган.")
    for r in rows:
        extra = "".join(x for x in (f", ⛔{r['no']}" if r["no"] else "", f", 👎{r['useless']}" if r["useless"] else "") if x)
        lines.append(f"• {escape(r['person'])} — {r['done']}/{r['total']}{extra}")
    return "\n".join(lines)
