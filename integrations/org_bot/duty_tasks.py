"""Daily tasks from the duties register — week 2 of automatic delegation (2026-11-02, the Boss's T-0051 / T-0055).

The register (``duties.py``) says who does what and how often; here it becomes each day's work:

  * **08:45** (the API's round, working days, once): each person's due duties become task instances
    (``duty_tasks``) — a daily duty for today; a weekly one for its weekday (or the week, Monday to Saturday);
    a monthly one for its day (or the month); a deadline in its last 7 days. «Қоида» / «зарур бўлганда» rows are
    the profile only. Then ONE card per person: «📋 Бугунги вазифаларингиз», numbered, three buttons a line —
    **✅** done (a duty with a target asks «нечта?» and keeps the number), **⛔** not done (asks why, one line),
    **👎** «фойдасиз» (the Boss: useless tasks are analysed). The card updates itself as items are answered.
  * **08:50** IT's check (Admin Bot, counts and duty titles — no targets or sums): how many people and tasks went
    out, who wasn't reached, whether the register was read at 07:30, yesterday's «фойдасиз» marks.
  * **17:30** one reminder card with what is still open today (only to those who have something open).
  * **22:00** a daily task still open is «бажарилмади» (missed); a weekly / monthly one when its period ends.
  * Nobody on a day off (/dam), outside their working week, on an excused day, or while the switch is off
    (``duty_switch``; no row = off — the Director's or the admin's ▶️ on /vazifalar starts it).
Polite «сиз», Uzbek Cyrillic; a work card (tasks keep their card format), no emoji in sentences.
"""

from __future__ import annotations

import calendar
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from integrations.common.config import settings
from integrations.common.db import execute, fetch_all, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import to_local, today_local
from integrations.org_bot import kpi
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = "duty-tasks"
log = setup_logging(AGENT)

PREFIX = "dt"            # dt:<instance id>:d|x|u
SWITCH_PREFIX = "dsw"    # dsw:on / dsw:off (OPS Manager — the Director; Admin Bot — the admin)
SEND_AT, IT_AT, EVENING_AT, CLOSE_AT = (8, 45), (8, 50), (17, 30), (22, 0)
ANSWER_HOURS = 3      # a reply to the bot's question counts this long
QUICK_MINUTES = 30    # an answer that isn't a reply only this soon, and only when it's short (2026-11-03 review:
#                       a daily report with a number in it, sent within 3 hours, was taken as the duty's number)
SHORT_NUMBER = re.compile(r"\s*\d+(?:[.,]\d+)?\s*(?:\S+\s*){0,3}")  # «3», «3 та», «8 та қилдим»
DEADLINE_LEAD_DAYS = 7
TASK_CADENCES = ("daily", "weekly", "monthly", "deadline")
WEEKDAY = {"душанба": 0, "сешанба": 1, "чоршанба": 2, "пайшанба": 3, "жума": 4, "шанба": 5, "якшанба": 6}
STATUS_MARK = {"open": "▫️", "done": "✅", "not_done": "⛔", "missed": "⛔"}


# ---------------------------------------------------------------------------------------------------- pure


def period(cadence: str, spec: str, day: date) -> tuple[date, date] | None:
    """The period a duty's task covers if it is due on ``day``; None when it isn't due that day."""
    spec = (spec or "").strip().lower()
    if cadence == "daily":
        return day, day
    if cadence == "weekly":
        monday = day - timedelta(days=day.weekday())
        if spec in WEEKDAY:
            target = monday + timedelta(days=WEEKDAY[spec])
            return (target, target) if target == day else None
        return monday, monday + timedelta(days=5)
    if cadence == "monthly":
        last = day.replace(day=calendar.monthrange(day.year, day.month)[1])
        if spec.isdigit():
            target = day.replace(day=min(int(spec), last.day))
            return (target, target) if target == day else None
        return day.replace(day=1), last
    if cadence == "deadline":
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", spec)
        if not m:
            return None
        try:
            due = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        start = due - timedelta(days=DEADLINE_LEAD_DAYS)
        return (start, due) if start <= day <= due else None
    return None


def when_text(t: dict[str, Any], day: date) -> str:
    """«(ҳафта охиригача)», «(ой охиригача)», «(муддат 24.12)» — nothing for a task of today."""
    if t["period_end"] == day:
        return f" (муддат {t['period_end']:%d.%m})" if t["cadence"] == "deadline" else ""
    if t["cadence"] == "weekly":
        return " (ҳафта охиригача)"
    if t["cadence"] == "monthly":
        return " (ой охиригача)"
    return f" (муддат {t['period_end']:%d.%m})"


def shown(t: dict[str, Any], day: date) -> bool:
    """Whether a card shows the task today: a month's task on Mondays, its first day and its last 5 days only
    (not the whole month every morning); everything else while it covers the day."""
    if t["cadence"] == "monthly" and t["period_start"] != t["period_end"]:
        return day.weekday() == 0 or day == t["period_start"] or (t["period_end"] - day).days < 5 or t["status"] != "open"
    return True


def aim_text(target: str, unit: str) -> str:
    """«2 та», «$1 350», «$15 000–20 000»."""
    if not target:
        return ""
    if unit.strip() == "$":
        nums = [f"{int(float(x)):,}".replace(",", " ") if x.replace(".", "", 1).isdigit() else x for x in re.split(r"[–-]", target)]
        return "$" + "–".join(nums)
    return f"{target} {unit}".strip()


def item_line(n: int, t: dict[str, Any], day: date) -> str:
    aim = f" — {aim_text(t['target'], t['unit'])}" if t.get("target") else ""
    done = ""
    if t["status"] == "done" and t.get("value") is not None:
        done = f" · бажарилди: {float(t['value']):g}"
    elif t["status"] in ("not_done", "missed") and t.get("reason"):
        done = f" · {escape(t['reason'][:80])}"
    if t.get("fact") is not None:
        fact = aim_text(f"{float(t['fact']):.0f}", "$")
        done += f" · {escape(t.get('fact_source') or 'тизим')}: {fact}"
    if t.get("mismatch"):
        done += " · ⚠️ тизимда тасдиқланмади"
    flag = " 👎" if t.get("useless") else ""
    return f"{STATUS_MARK[t['status']]} {n}. {escape(t['title'])}{escape(aim)}{escape(when_text(t, day))}{done}{flag}"


def card(tasks: list[dict[str, Any]], day: date, evening: bool = False) -> tuple[str, dict[str, Any]]:
    """The card and its buttons: three per open line (✅ ⛔ 👎), a 👎 alone once answered."""
    head = "📋 <b>Бугун қолган вазифалар</b>" if evening else "📋 <b>Бугунги вазифаларингиз</b>"
    lines = [f"{head} · {day:%d.%m}", ""]
    rows: list[list[dict[str, str]]] = []
    tasks = [t for t in tasks if shown(t, day)]
    for n, t in enumerate(tasks, start=1):
        lines.append(item_line(n, t, day))
        if t["status"] == "open":
            rows.append([{"text": f"{n} ✅", "callback_data": f"{PREFIX}:{t['id']}:d"},
                         {"text": f"{n} ⛔", "callback_data": f"{PREFIX}:{t['id']}:x"},
                         {"text": f"{n} 👎", "callback_data": f"{PREFIX}:{t['id']}:u"}])
    lines += ["", "<i>✅ бажарилди · ⛔ бўлмади · 👎 фойдасиз вазифа (реестрни тузатиш учун)</i>"]
    return "\n".join(lines), {"inline_keyboard": rows}


def parse_value(text: str) -> float | None:
    """«3», «3 та», «2,5» → the number; anything else → None."""
    m = re.search(r"\d+(?:[.,]\d+)?", (text or "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


# ---------------------------------------------------------------------------------------------------- state


async def is_on() -> bool:
    row = await fetch_one("SELECT enabled FROM duty_switch")
    return bool(row and row["enabled"])


async def set_switch(enabled: bool, by: str, from_admin: bool, run_id: uuid.UUID) -> None:
    """On / off; the other side is told (the Director when the admin did it, and back) — no figures."""
    await execute("""INSERT INTO duty_switch (id, enabled, set_by) VALUES (TRUE, %s, %s)
                     ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = now()""",
                  (enabled, by))
    await log_action(agent=AGENT, action="switch_on" if enabled else "switch_off", target_system="postgres",
                     status="success", run_id=run_id, mode="write", payload={"by_admin": from_admin})
    word = "ёқди — 08:45 да ҳар бир ходимга реестрдан бугунги вазифалари юборилади" if enabled else "тўхтатди"
    if from_admin:
        from integrations.org_bot import store
        from integrations.org_bot.roles import DIRECTOR_ROLE

        for boss in await store.active_employees_by_role(DIRECTOR_ROLE):
            try:
                await _send(boss["telegram_user_id"], f"📋 IT ({escape(by)}) кунлик вазифаларни {word}. /vazifalar", run_id)
            except TelegramError as exc:
                log.warning("Director not told about the duty switch: {}", type(exc).__name__)
    else:
        from integrations.org_bot import admin

        await admin.tell_admin(f"📋 {escape(by)} кунлик вазифаларни {word}.", run_id)


def switch_button(enabled: bool) -> dict[str, str]:
    return ({"text": "⏸ Кунлик вазифаларни тўхтатиш", "callback_data": f"{SWITCH_PREFIX}:off"} if enabled
            else {"text": "▶️ Кунлик вазифаларни бошлаш", "callback_data": f"{SWITCH_PREFIX}:on"})


async def claim(day: date, kind: str) -> bool:
    """Once a day per step (send / it / evening / close)."""
    return await fetch_one("INSERT INTO duty_runs (day, kind) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING day",
                           (day, kind)) is not None


async def people_today(day: date) -> list[dict[str, Any]]:
    """Active people with duties in the register who work today and aren't excused."""
    rows = await fetch_all(
        """SELECT DISTINCT e.* FROM employees e JOIN duties d ON d.employee_id = e.id AND d.active
           WHERE e.status = 'active' AND NOT EXISTS (SELECT 1 FROM excused_days x WHERE x.employee_id = e.id AND x.day = %s)""",
        (day,))
    return [e for e in rows if kpi.works_on(e, day)]


async def generate(day: date, only: Any = None) -> int:
    """Today's task instances from the register; how many new. ``only``: one person's (a test — nobody else's
    score, card or 22:00 close is touched)."""
    made = 0
    for person in await people_today(day):
        if only is not None and str(person["id"]) != str(only):
            continue
        for d in await fetch_all("SELECT * FROM duties WHERE active AND employee_id = %s AND cadence = ANY(%s)",
                                 (person["id"], list(TASK_CADENCES))):
            span = period(d["cadence"], d["day"], day)
            if span is None:
                continue
            row = await fetch_one(
                """INSERT INTO duty_tasks (duty_id, employee_id, period_start, period_end, cadence, title, target, unit)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (duty_id, period_start) DO NOTHING RETURNING id""",
                (d["id"], person["id"], span[0], span[1], d["cadence"], d["title"], d["target"], d["unit"]))
            made += row is not None
    return made


async def tasks_for(employee_id: Any, day: date, *, open_only: bool = False) -> list[dict[str, Any]]:
    """The tasks a card shows: those covering ``day`` — open ones, and those answered that day."""
    return await fetch_all(
        f"""SELECT * FROM duty_tasks WHERE employee_id = %s AND period_start <= %s AND period_end >= %s
              AND (status = 'open' {'' if open_only else "OR (answered_at AT TIME ZONE 'Asia/Tashkent')::date = %s"})
            ORDER BY CASE cadence WHEN 'daily' THEN 0 WHEN 'deadline' THEN 1 WHEN 'weekly' THEN 2 ELSE 3 END, id""",
        (employee_id, day, day) if open_only else (employee_id, day, day, day))


# ---------------------------------------------------------------------------------------------------- Telegram


def _bot(run_id: uuid.UUID | None) -> TelegramBot:
    return TelegramBot(agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def _send(chat_id: int, text: str, run_id: uuid.UUID, keyboard: dict[str, Any] | None = None) -> int | None:
    async with _bot(run_id) as bot:
        ids = await bot.send_message(text, chat_id=str(chat_id), reply_markup=keyboard)
    return ids[-1] if ids else None


async def _answer(callback: dict[str, Any], text: str) -> None:
    async with _bot(None) as bot:
        await bot._answer_callback(callback.get("id", ""), text)  # noqa: SLF001


async def send_cards(day: date, run_id: uuid.UUID, *, evening: bool = False, only: Any = None) -> dict[str, Any]:
    """One card per person (morning: everything due; evening: only those with something still open)."""
    sent, failed, tasks = 0, [], 0
    for person in await people_today(day):
        if only is not None and str(person["id"]) != str(only):
            continue
        mine = [t for t in await tasks_for(person["id"], day, open_only=evening) if shown(t, day)]
        if not mine:
            continue
        text, keyboard = card(mine, day, evening)
        try:
            message_id = await _send(person["telegram_user_id"], text, run_id, keyboard)
        except TelegramError as exc:
            failed.append(person.get("full_name") or person.get("display_name") or "?")
            log.warning("Duty card not delivered: {}", type(exc).__name__)
            continue
        await execute("""INSERT INTO duty_cards (employee_id, day, kind, message_id) VALUES (%s, %s, %s, %s)
                         ON CONFLICT (employee_id, day, kind) DO UPDATE SET message_id = EXCLUDED.message_id, sent_at = now()""",
                      (person["id"], day, "evening" if evening else "morning", message_id))
        sent += 1
        tasks += len(mine)
    await log_action(agent=AGENT, action="evening_cards" if evening else "morning_cards", target_system="telegram",
                     status="success" if not failed else "partial", run_id=run_id, mode="notify",
                     payload={"people": sent, "tasks": tasks, "failed": len(failed)})
    return {"people": sent, "tasks": tasks, "failed": failed}


async def _refresh(callback: dict[str, Any], employee_id: Any, run_id: uuid.UUID) -> None:
    """Redraw the tapped card from the database."""
    message = callback.get("message") or {}
    chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
    if not (chat_id and message_id):
        return
    day = to_local(datetime.fromtimestamp(message["date"], tz=timezone.utc)).date() if message.get("date") else today_local()
    text, keyboard = card(await tasks_for(employee_id, day), day, "қолган" in (message.get("text") or ""))
    async with _bot(run_id) as bot:
        try:
            await bot._edit_message(chat_id=str(chat_id), message_id=message_id, text=text, reply_markup=keyboard)  # noqa: SLF001
        except TelegramError as exc:
            log.warning("Duty card not redrawn: {}", type(exc).__name__)


async def handle_callback(prefix: str, rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """✅ / ⛔ / 👎 on a card; the Director's ⏸ / ▶️."""
    if prefix not in (PREFIX, SWITCH_PREFIX):
        return None
    from integrations.org_bot import store
    from integrations.org_bot.roles import DIRECTOR_ROLE

    clicker_id = (callback.get("from") or {}).get("id")
    clicker = await store.get_employee_by_telegram_id(clicker_id) if clicker_id else None
    if clicker is None or clicker.get("status") != "active":
        await _answer(callback, "Рухсат йўқ")
        return "duty_unauthorized"
    if prefix == SWITCH_PREFIX:
        if clicker.get("role") != DIRECTOR_ROLE:
            await _answer(callback, "Рухсат йўқ")
            return "duty_unauthorized"
        from integrations.org_bot import names

        await set_switch(rest == "on", names.person_name(clicker), False, run_id)
        await _answer(callback, "▶️ Ёқилди" if rest == "on" else "⏸ Тўхтатилди")
        from integrations.org_bot import duties

        message = callback.get("message") or {}
        if message.get("message_id"):
            text, keyboard = await duties.director_card()
            async with _bot(run_id) as bot:
                try:
                    await bot._edit_message(chat_id=str((message.get("chat") or {}).get("id")),  # noqa: SLF001
                                            message_id=message["message_id"], text=text, reply_markup=keyboard)
                except TelegramError as exc:
                    log.warning("Register view not redrawn: {}", type(exc).__name__)
        return "duty_switch_on" if rest == "on" else "duty_switch_off"
    task_id, _, value = rest.partition(":")
    if value not in ("d", "x", "u"):  # only our three buttons (a forged value never closes a task)
        await _answer(callback, "Номаълум амал")
        return "duty_unknown"
    task = await fetch_one("SELECT * FROM duty_tasks WHERE id = %s", (int(task_id),)) if task_id.isdigit() else None
    if task is None or str(task["employee_id"]) != str(clicker["id"]):
        await _answer(callback, "Рухсат йўқ")
        return "duty_unauthorized"
    if value == "u":
        await execute("UPDATE duty_tasks SET useless = NOT useless, useless_at = now() WHERE id = %s", (task["id"],))
        await _answer(callback, "👎 фойдасиз деб белгиланди" if not task["useless"] else "белги олиб ташланди")
        await _refresh(callback, clicker["id"], run_id)
        return "duty_useless"
    if task["status"] != "open":
        await _answer(callback, "Жавоб аллақачон берилган")
        return "duty_already_answered"
    # Answered at once (a forgotten number never turns a done task into «missed»); the number / reason follows.
    kind = None if value == "d" and not task.get("target") else ("value" if value == "d" else "reason")
    await execute("UPDATE duty_tasks SET status = %s, answered_at = now(), asked_kind = %s, asked_at = now() WHERE id = %s",
                  ("done" if value == "d" else "not_done", kind, task["id"]))
    await _answer(callback, "✅" if value == "d" else "⛔")
    await _refresh(callback, clicker["id"], run_id)
    if kind is None:
        return "duty_done"
    ask = (f"«{escape(task['title'])}» — нечта бўлди? Рақам билан ёзинг (мақсад: {escape(aim_text(task['target'], task['unit']))})."
           if kind == "value" else f"«{escape(task['title'])}» — нима сабабдан бўлмади? Бир жумлада ёзинг.")
    asked = await _send(clicker["telegram_user_id"], ask, run_id)
    await execute("UPDATE duty_tasks SET asked_message_id = %s WHERE id = %s", (asked, task["id"]))
    return "duty_value_asked" if kind == "value" else "duty_reason_asked"


async def take_message(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """After ✅ on a duty with a target — the number; after ⛔ — the reason. None when nothing waits."""
    text = (message.get("text") or "").strip()
    if not text or text.startswith("/"):
        return None
    try:
        task = await fetch_one(
            """SELECT * FROM duty_tasks WHERE employee_id = %s AND asked_kind IS NOT NULL
                 AND asked_at > now() - make_interval(hours => %s) ORDER BY asked_at DESC LIMIT 1""",
            (employee["id"], ANSWER_HOURS))
    except Exception as exc:  # noqa: BLE001 — the message goes on to the usual flow
        log.warning("Duty question state not read: {}", type(exc).__name__)
        return None
    if task is None:
        return None
    replied = (message.get("reply_to_message") or {}).get("message_id")
    is_reply = bool(replied) and replied == task.get("asked_message_id")
    if replied and not is_reply:
        return None  # a reply to another message (the 16:00 report ask, a task card) is that, not this
    quick = task.get("asked_at") is not None and (datetime.now(timezone.utc) - task["asked_at"]).total_seconds() <= QUICK_MINUTES * 60
    if task["asked_kind"] == "value":
        value = parse_value(text)
        if value is None or not (is_reply or (quick and len(text) <= 40 and SHORT_NUMBER.fullmatch(text))):
            return None  # words, a long message or a late one: the usual flow (the question stays)
        await execute("UPDATE duty_tasks SET value = %s, asked_kind = NULL WHERE id = %s", (value, task["id"]))
        await _facts(today_local())  # a typed dollar figure is checked against SAP / Billz at once
        await _send(employee["telegram_user_id"], f"Раҳмат, ёзиб қўйдим: «{escape(task['title'])}» — {value:g}.", run_id)
        return "duty_value_saved"
    if ("?" in text and not is_reply) or not (is_reply or (quick and len(text) <= 160 and "ҳисобот" not in text.lower())):
        return None
    await execute("UPDATE duty_tasks SET reason = %s, asked_kind = NULL WHERE id = %s", (text[:300], task["id"]))
    await _send(employee["telegram_user_id"], "Раҳмат, сабабини ёзиб қўйдим.", run_id)
    return "duty_reason_saved"


# ---------------------------------------------------------------------------------------------------- the day


async def _facts(day: date) -> None:
    """The system's figures for today's dollar targets (plan_facts.py, week 3); a failure never stops the cards."""
    from integrations.org_bot import plan_facts

    try:
        await plan_facts.fill_duty_facts(day)
    except Exception as exc:  # noqa: BLE001
        log.warning("Duty facts not filled: {}", type(exc).__name__)


async def test_send(employee: dict[str, Any], run_id: uuid.UUID) -> str:
    """Admin Bot «/vazifalar sinov»: today's card to this one person only — even while the switch is off — and the
    08:45 IT check as it would read. Their tasks are real (the buttons work); nobody else's are made."""
    from integrations.org_bot import admin

    day = today_local()
    await generate(day, only=employee["id"])
    result = await send_cards(day, run_id, only=employee["id"])
    await admin.tell_admin("🧪 <b>Синов</b> — карта фақат сизга юборилди.\n\n" + await it_check(day, result, run_id), run_id)
    return "duty_test_sent" if result["people"] else "duty_test_nothing"


async def close_day(day: date) -> int:
    """22:00: today's tasks still open, and longer ones whose period ended, are «бажарилмади»."""
    rows = await fetch_all("UPDATE duty_tasks SET status = 'missed', asked_kind = NULL WHERE status = 'open' AND period_end <= %s "
                           "RETURNING id", (day,))
    return len(rows)


async def it_check(day: date, morning: dict[str, Any], run_id: uuid.UUID) -> str:
    """08:50 to IT: did the agent collect and send — counts, names of the unreached, duty titles marked useless."""
    synced = await fetch_one("SELECT max(at) AS at FROM duty_changes WHERE change = 'synced'")
    read = synced and synced["at"] and to_local(synced["at"]).date() == day
    useless = await fetch_all(
        """SELECT t.title, coalesce(nullif(btrim(e.full_name), ''), e.display_name) AS person FROM duty_tasks t
           JOIN employees e ON e.id = t.employee_id
           WHERE t.useless AND (t.useless_at AT TIME ZONE 'Asia/Tashkent')::date = %s""", (day - timedelta(days=1),))
    lines = [f"🤖 <b>Агент · {day:%d.%m} 08:45</b>",
             f"{'✅' if read else '❌'} Реестр 07:30 да ўқилди" + ("" if read else " — ўқилмади, /vazifalar"),
             f"{'✅' if morning['people'] else '⚠️'} {morning['people']} кишига {morning['tasks']} та вазифа юборилди"]
    if morning["failed"]:
        lines.append("❌ Етмади (ботни блоклаган ёки чат ёпиқ): " + ", ".join(escape(n) for n in morning["failed"]))
    if useless:
        lines += ["", "<b>Кеча «фойдасиз» деб белгиланганлар:</b>"] + [f"• {escape(u['title'])} — {escape(u['person'])}" for u in useless]
    return "\n".join(lines)


async def tick(now: datetime) -> str | None:
    """The API's 5-minute round: 08:45 cards (+ 08:50 IT check), 17:30 reminder, 22:00 close."""
    day = now.date()
    hm = (now.hour, now.minute)
    if hm >= CLOSE_AT:
        if await claim(day, "close"):
            await _facts(day)
            await close_day(day)
            return "closed"
        return None
    if settings.bots_frozen or not await is_on():
        return None
    from integrations.org_bot import store

    if await store.is_day_off(day):
        return None
    run_id = uuid.uuid4()
    if SEND_AT <= hm < (12, 0) and await claim(day, "send"):
        await generate(day)
        morning = await send_cards(day, run_id)
        from integrations.org_bot import admin

        await admin.tell_admin(await it_check(day, morning, run_id), run_id)
        return "sent"
    if EVENING_AT <= hm < (20, 0) and await claim(day, "evening"):
        await _facts(day)
        await send_cards(day, run_id, evening=True)
        return "evening"
    return None


async def progress(day: date) -> list[dict[str, Any]]:
    """Today per person: tasks covering today, how many answered done — for the Director's /vazifalar."""
    return await fetch_all(
        """SELECT coalesce(nullif(btrim(e.full_name), ''), e.display_name) AS person, count(*) AS total,
                  count(*) FILTER (WHERE t.status = 'done') AS done, count(*) FILTER (WHERE t.useless) AS useless
           FROM duty_tasks t JOIN employees e ON e.id = t.employee_id
           WHERE t.period_start <= %s AND t.period_end >= %s GROUP BY 1 ORDER BY 1""", (day, day))
