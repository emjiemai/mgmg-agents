"""The Friday review of the daily tasks — week 3 of automatic delegation (2026-11-02, the Boss's T-0055).

«Ҳар жума куни агент ишини ҳафталик натижалар асосида тузатинг» and «фойдасиз деб баҳоланган топшириқларни таҳлил
қилинг». Every Friday at 16:30 (the API's round, once):

  * **the week per person** — tasks, done, not done, missed, «фойдасиз», a typed number the system didn't confirm;
  * **duties that need a look** — chosen by rules, never by the AI (the МИЯ document: rules count, the AI explains):
    marked «фойдасиз» twice or more, done in under half of at least three days, a number above the system's fact;
  * **a suggested change for each** — the AI reads the week's reasons and writes one short, polite change (a target,
    how often, a time, merge or remove) and why; without the AI, plain words from the rule. Never about pay or people;
  * **the plans** — /reja's month so far;
  * to **the Director** with ✅ Қабул / ❌ Рад per suggestion (accepted ones go to IT to change in the register — the
    register stays people's own document); to **IT** the same without amounts.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.db import fetch_all, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = "duty-review"
log = setup_logging(AGENT)

PREFIX = "dsg"           # dsg:<suggestion id>:y|n
COMMANDS = ("/tahlil", "/таҳлил")  # the Director: the week's review so far, now (2026-11-03)
REVIEW_AT = (16, 30)
FRIDAY = 4
MIN_DAYS = 3             # a duty is judged on at least this many tasks
LOW_SHARE = 0.5
USELESS_MARKS = 2
MAX_SUGGESTIONS = 6

SYSTEM = """\
You help the operations manager of MGMG (Tashkent) tune employees' recurring duties after a week of data.
For each duty given (person, duty, how often, target, the week's result, the reasons people wrote, why it was
flagged), write ONE concrete change to the duty itself — a lower or higher target, another frequency or time,
merging it with another duty, or removing it — and one short reason based only on the facts given.
Never suggest anything about pay, penalties, firing or a person's character. Never invent numbers.
Uzbek, Cyrillic script, polite «сиз» form, each text at most 160 characters, no emoji.
Answer JSON only: {"items": [{"n": 1, "change": "...", "why": "..."}]}
"""


# ---------------------------------------------------------------------------------------------------- pure


def week_of(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def person_stats(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for t in tasks:
        s = by.setdefault(t["person"], {"person": t["person"], "total": 0, "done": 0, "not_done": 0, "missed": 0,
                                        "open": 0, "useless": 0, "mismatch": 0})
        s["total"] += 1
        s[t["status"]] += 1
        s["useless"] += bool(t.get("useless"))
        s["mismatch"] += bool(t.get("mismatch"))
    return sorted(by.values(), key=lambda s: (s["done"] / s["total"] if s["total"] else 1, s["person"]))


def flagged(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The duties that need a look, by rule: «фойдасиз» ≥ 2, done < 50 % of ≥ 3, a number above the system's fact."""
    by: dict[Any, dict[str, Any]] = {}
    for t in tasks:
        d = by.setdefault(t["duty_id"], {"duty_id": t["duty_id"], "person": t["person"], "title": t["title"],
                                         "cadence": t["cadence"], "target": t.get("target") or "", "unit": t.get("unit") or "",
                                         "total": 0, "done": 0, "useless": 0, "mismatch": 0, "reasons": []})
        d["total"] += 1
        d["done"] += t["status"] == "done"
        d["useless"] += bool(t.get("useless"))
        d["mismatch"] += bool(t.get("mismatch"))
        if t.get("reason") and len(d["reasons"]) < 4:
            d["reasons"].append(t["reason"])
    out = []
    for d in by.values():
        why = []
        if d["useless"] >= USELESS_MARKS:
            why.append("useless")
        if d["total"] >= MIN_DAYS and d["done"] / d["total"] < LOW_SHARE:
            why.append("low")
        if d["mismatch"]:
            why.append("mismatch")
        if why:
            out.append({**d, "why": why})
    order = {"useless": 0, "mismatch": 1, "low": 2}
    return sorted(out, key=lambda d: (min(order[w] for w in d["why"]), d["done"] / max(d["total"], 1)))[:MAX_SUGGESTIONS]


def rule_text(d: dict[str, Any]) -> tuple[str, str]:
    """The suggestion in plain words, when the AI isn't there."""
    if "useless" in d["why"]:
        return ("вазифани реестрдан олиб ташлаш ёки мазмунини ўзгартиришни кўриб чиқиш",
                f"ходим ҳафтада {d['useless']} марта «фойдасиз» деб белгилади")
    if "mismatch" in d["why"]:
        return ("бу вазифани фақат тизимдаги рақам бўйича ҳисоблаш",
                "ёзилган рақам тизимдаги фактдан баланд — тизимда тасдиқланмади")
    return ("мақсадни ёки қанча тез-тез бажарилишини қайта кўриб чиқиш",
            f"ҳафтада {d['total']} кундан {d['done']} тасида бажарилди")


def ai_prompt(items: list[dict[str, Any]]) -> str:
    words = {"useless": "marked useless by the person", "low": "done on under half of the days", "mismatch": "typed number above the system's fact"}
    lines = []
    for n, d in enumerate(items, start=1):
        aim = f", target {d['target']} {d['unit']}".rstrip() if d["target"] else ""
        lines.append(f"{n}. {d['person']} — «{d['title']}» ({d['cadence']}{aim}); done {d['done']} of {d['total']}; "
                     f"useless marks {d['useless']}; flagged: {', '.join(words[w] for w in d['why'])}; "
                     f"reasons written: {'; '.join(d['reasons']) or 'none'}")
    return "\n".join(lines)


def parse_ai(raw: str, count: int) -> dict[int, tuple[str, str]]:
    """The AI's changes by number — only polite, mostly-Cyrillic, short ones."""
    from integrations.org_bot.tone import is_polite

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    out = {}
    for item in (data.get("items") if isinstance(data, dict) else None) or []:
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        change, why = " ".join(str(item.get("change") or "").split()), " ".join(str(item.get("why") or "").split())
        text = change + " " + why
        letters = re.findall(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]", text)
        cyr = re.findall(r"[А-Яа-яЁёЎўҚқҒғҲҳ]", text)
        if 1 <= n <= count and change and why and len(change) <= 200 and len(why) <= 200 and "<" not in text \
                and len(cyr) >= 0.7 * len(letters) and is_polite(text):
            out[n] = (change, why)
    return out


def review_text(monday: date, end: date, people: list[dict[str, Any]], plans: str | None, for_it: bool) -> str:
    lines = [f"📊 <b>Ҳафталик вазифалар таҳлили</b> · {monday:%d.%m}–{end:%d.%m}", ""]
    if not people:
        lines.append("Бу ҳафта реестрдан вазифа берилмади.")
    for s in people:
        share = s["done"] / s["total"] if s["total"] else 0
        extra = "".join(x for x in (f", 👎{s['useless']}" if s["useless"] else "",
                                    f", ⚠️{s['mismatch']} тасдиқланмади" if s["mismatch"] else "") if x)
        lines.append(f"{'🟢' if share >= 0.8 else '🟡' if share >= 0.6 else '🔴'} {escape(s['person'])} — "
                     f"{s['done']}/{s['total']} ({share:.0%}){extra}")
    if plans and not for_it:
        lines += ["", plans]
    return "\n".join(lines)


def suggestion_text(n: int, s: dict[str, Any]) -> str:
    return f"{n}. <b>{escape(s['person'])}</b> — «{escape(s['title'])}»\n   ➜ {escape(s['change'])}\n   <i>{escape(s['why'])}</i>"


# ---------------------------------------------------------------------------------------------------- runtime


def _bot(run_id: uuid.UUID | None) -> TelegramBot:
    return TelegramBot(agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def week_tasks(monday: date, end: date) -> list[dict[str, Any]]:
    return await fetch_all(
        """SELECT t.*, coalesce(nullif(btrim(e.full_name), ''), e.display_name) AS person FROM duty_tasks t
           JOIN employees e ON e.id = t.employee_id
           WHERE t.period_start <= %s AND t.period_end >= %s AND e.status = 'active'""", (end, monday))


async def suggestions(items: list[dict[str, Any]], run_id: uuid.UUID) -> list[tuple[str, str]]:
    written: dict[int, tuple[str, str]] = {}
    if items:
        from integrations.ai.openrouter_client import OpenRouterClient

        try:
            async with OpenRouterClient(agent=AGENT, run_id=run_id) as ai:
                written = parse_ai(await ai.complete(SYSTEM, ai_prompt(items), json_mode=True), len(items))
        except Exception as exc:  # noqa: BLE001 — the rule's own words instead
            log.warning("AI suggestions failed: {}", type(exc).__name__)
    return [written.get(n, rule_text(d)) for n, d in enumerate(items, start=1)]


async def run(day: date, run_id: uuid.UUID, only_chat: int | None = None) -> dict[str, int]:
    """The review: stats, flagged duties with suggestions (kept in duty_suggestions), to the Director and IT.
    ``only_chat``: the Director's «/tahlil» — the review so far, to him only (IT isn't sent a copy)."""
    from integrations.org_bot import admin, plan_facts, store
    from integrations.org_bot.roles import DIRECTOR_ROLE

    monday, _sunday = week_of(day)
    try:
        await plan_facts.fill_duty_facts(day)
    except Exception as exc:  # noqa: BLE001 — the review goes out without the system's facts
        log.warning("Facts not filled: {}", type(exc).__name__)
    tasks = await week_tasks(monday, day)
    people = person_stats(tasks)
    items = flagged(tasks)
    written = await suggestions(items, run_id)
    stored = []
    for d, (change, why) in zip(items, written):
        row = await fetch_one(
            """INSERT INTO duty_suggestions (week, duty_id, person, title, change, why) VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING *""", (monday, d["duty_id"], d["person"], d["title"], change, why))
        stored.append(row)
    try:
        plans = plan_facts.plans_text(await plan_facts.plans_vs_facts(day), day)
    except Exception as exc:  # noqa: BLE001
        log.warning("Plans not read: {}", type(exc).__name__)
        plans = None
    from integrations.org_bot import rollout

    try:
        connected = "\n\n" + rollout.status_text(await rollout.status(day))
    except Exception as exc:  # noqa: BLE001
        log.warning("Connection status not read: {}", type(exc).__name__)
        connected = ""
    keyboard = {"inline_keyboard": [[{"text": f"{n} ✅ Қабул", "callback_data": f"{PREFIX}:{s['id']}:y"},
                                     {"text": f"{n} ❌ Рад", "callback_data": f"{PREFIX}:{s['id']}:n"}]
                                    for n, s in enumerate(stored, start=1)]}
    body = "\n\n".join(suggestion_text(n, s) for n, s in enumerate(stored, start=1))
    tail = ("\n\n<b>Реестрга таклифлар</b> (қоида танлади, AI ёзди — қабул қилинганини IT реестрда ўзгартиради)\n\n" + body
            if stored else "\n\nБу ҳафта реестрни ўзгартириш таклифи йўқ.")
    async with _bot(run_id) as bot:
        bosses = [{"telegram_user_id": only_chat}] if only_chat else await store.active_employees_by_role(DIRECTOR_ROLE)
        for boss in bosses:
            try:
                await bot.send_message(review_text(monday, day, people, plans, False) + connected + tail, chat_id=str(boss["telegram_user_id"]),
                                       reply_markup=keyboard if stored else None)
            except TelegramError as exc:
                log.warning("Review not delivered: {}", type(exc).__name__)
    if not only_chat:
        await admin.tell_admin(review_text(monday, day, people, None, True) + connected + tail.replace("<b>Реестрга таклифлар</b>", "<b>Таклифлар</b>"), run_id)
    counts = {"people": len(people), "tasks": len(tasks), "suggestions": len(stored)}
    await log_action(agent=AGENT, action="weekly_review", target_system="telegram", status="success", run_id=run_id,
                     mode="notify", payload=counts)
    return counts


async def tick(now: Any) -> dict[str, int] | None:
    """Fridays at 16:30, once (while the daily tasks are on)."""
    from integrations.org_bot import duty_tasks

    if now.weekday() != FRIDAY or (now.hour, now.minute) < REVIEW_AT or now.hour >= 20 or settings.bots_frozen:
        return None
    if not await duty_tasks.is_on() or not await duty_tasks.claim(now.date(), "review"):
        return None
    return await run(now.date(), uuid.uuid4())


async def handle_callback(prefix: str, rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """The Director's ✅ Қабул / ❌ Рад on a suggestion; an accepted one goes to IT to change in the register."""
    if prefix != PREFIX:
        return None
    from integrations.org_bot import admin, names, store
    from integrations.org_bot.roles import DIRECTOR_ROLE

    clicker = await store.get_employee_by_telegram_id((callback.get("from") or {}).get("id"))
    sid, _, choice = rest.partition(":")
    async with _bot(None) as bot:
        if not (clicker and clicker.get("status") == "active" and clicker.get("role") == DIRECTOR_ROLE and sid.isdigit()):
            await bot._answer_callback(callback.get("id", ""), "Рухсат йўқ")  # noqa: SLF001
            return "suggestion_unauthorized"
        row = await fetch_one(
            """UPDATE duty_suggestions SET status = %s, decided_by = %s, decided_at = now()
               WHERE id = %s AND status = 'pending' RETURNING *""",
            ("accepted" if choice == "y" else "rejected", names.person_name(clicker), int(sid)))
        if row is None:
            await bot._answer_callback(callback.get("id", ""), "Аллақачон ҳал қилинган")  # noqa: SLF001
            return "suggestion_already_decided"
        await bot._answer_callback(callback.get("id", ""), "✅ Қабул" if choice == "y" else "❌ Рад")  # noqa: SLF001
    if choice == "y":
        await admin.tell_admin(f"📋 Директор қабул қилди — реестрда ўзгартиринг:\n<b>{escape(row['person'])}</b> — "
                               f"«{escape(row['title'])}»\n➜ {escape(row['change'])}", run_id)
    return "suggestion_accepted" if choice == "y" else "suggestion_rejected"
