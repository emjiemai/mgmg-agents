"""22:00 — daily reports close; no report costs KPI points (2026-10-15, the Director).

The Director: reports until 22:00; whoever hasn't sent one gets minus points,
every day, and is told so. At 22:00 Tashkent every ask of the day still open
gets ``REPORT_MISS_PENALTY`` points (``daily_reports.penalty_points``, once —
``penalized_at``), which come off that month's KPI total (``kpi_score.py``),
and the person gets one polite line with the minus and the month's sum.

Runs inside the API service (``app.py`` checks every 5 minutes), so it needs
no cron of its own. People who don't report by agreement (📝 off) and days
off are never asked, so never here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from integrations.common.config import settings
from integrations.common.db import fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local, to_local
from integrations.org_bot import kpi, names, store
from integrations.telegram.bot import TelegramBot, TelegramError

AGENT = "report-close"
# The first day that costs points: 15.10's 16:00 ask didn't say «22:00» yet, so nobody
# loses points for a rule they weren't told (asks from 16.10 say it).
PENALTY_FROM = date(2026, 10, 16)
log = setup_logging(AGENT)


CLEANUP_ACTION = "dayoff_misses_cleared"


async def clear_days_off_once() -> int:
    """Once (2026-10-22, the owner): the red cells and minus points on Garmin сотув days off, asked before their
    own weeks were set, are removed. Marked in agent_actions so it never runs again; counts only."""
    done = await fetch_one("SELECT 1 AS x FROM agent_actions WHERE agent = %s AND action = %s AND status = 'success' LIMIT 1",
                           (AGENT, CLEANUP_ACTION))
    if done is not None:
        return 0
    removed = await store.delete_missed_on_days_off()
    await log_action(agent=AGENT, action=CLEANUP_ACTION, target_system="postgres", status="success", mode="write",
                     payload={"removed": removed})
    log.info("Removed {} unsent report ask(s) on people's days off", removed)
    return removed


# The report from the day's messages (2026-10-22, the owner: "employees might forget to press the button —
# human error"): before a report counts as missed, what the person sent that day but never confirmed is read.
COMPOSE_PROMPT = """An employee of MGMG (Tashkent) owed today's work report but never pressed the \
"this is my report" button. Below is everything they sent the work bot today, with times: messages \
left unconfirmed, files (voice and video already as text), and what they wrote to the work AI.

Decide whether it tells what they DID at work today. If it does, write their report from it: \
only their own facts (tasks done, customers, places, numbers), nothing added or guessed, short \
(at most 6 lines, "• " per item), Uzbek in Cyrillic script, as their own report ("… қилдим"). \
Questions to the AI, greetings, chatter or plans alone are NOT a report.

Answer as JSON only: {"report": "<the report>"} or {"report": null}."""
COMPOSE_MAX_CHARS = 900
AUTO_PREFIX = "[чатдан тузилди] "


def chat_lines(chat: dict[str, list[dict[str, Any]]], file_texts: dict[str, str]) -> list[str]:
    """The day's unconfirmed messages as «HH:MM [where] text» lines, in time order."""
    from integrations.org_bot import report_media

    items: list[tuple[Any, str]] = []
    items += [(r["created_at"], f"[unconfirmed message] {r['text']}") for r in chat.get("held", [])]
    items += [(r["created_at"], f"[to the work AI] {r['text']}") for r in chat.get("ai", [])]
    for f in chat.get("files", []):
        text = file_texts.get(str(f["id"])) or report_media.placeholder(f["kind"], f.get("caption"))
        items.append((f["created_at"], f"[file] {text}"))
    return [f"{to_local(at):%H:%M} {' '.join(str(text).split())[:800]}" for at, text in sorted(items, key=lambda x: x[0])]


async def _compose(lines: list[str], run_id: uuid.UUID) -> str | None:
    from integrations.ai.openrouter_client import OpenRouterClient

    async with OpenRouterClient(agent=AGENT, run_id=run_id, model_override=settings.ops_manager_bot_model,
                                fallback_override=settings.ops_manager_bot_fallback_models) as ai:
        result = await ai.complete_json(COMPOSE_PROMPT, "\n".join(lines))
    report = result.get("report") if isinstance(result, dict) else None
    report = str(report).strip() if report else ""
    return report[:COMPOSE_MAX_CHARS] or None


async def build_from_chat(day: date) -> int:
    """22:00, before the misses: each owed report the person's own unconfirmed messages of the day can fill
    is filled — sent, no minus, marked ``auto_built``, its files kept with it, the person told. How many."""
    from integrations.org_bot import ops_manager, report_media

    built = 0
    run_id = uuid.uuid4()
    for row in await store.owed_reports(day):
        try:
            chat = await store.day_chat(row["telegram_user_id"], day)
            if not any(chat.values()):
                continue
            texts: dict[str, str] = {}
            for f in chat["files"]:
                if f.get("file_id") and f["kind"] in (*report_media.LISTEN_KINDS, *report_media.WATCH_KINDS) \
                        and report_media.readable(f["kind"], int(f.get("file_size") or 0)):
                    try:
                        texts[str(f["id"])] = report_media.report_line(f["kind"], await ops_manager._file_text(f, run_id))
                    except Exception as exc:  # noqa: BLE001 — the rest still counts
                        log.warning("A held file not read as text: {}", type(exc).__name__)
            report = await _compose(chat_lines(chat, texts), run_id)
            if not report:
                continue
            saved = await store.mark_auto_report(str(row["id"]), AUTO_PREFIX + report)
            if saved is None:
                continue
            for file_row_id, text in texts.items():
                await store.set_file_text(file_row_id, text)
            await store.attach_held_files([str(f["id"]) for f in chat["files"]], str(row["id"]))
            built += 1
            async with TelegramBot(agent=AGENT, run_id=run_id,
                                   bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
                await bot.send_message(auto_built_text(names.call_name(row), report), chat_id=str(row["telegram_user_id"]))
        except Exception as exc:  # noqa: BLE001 — that person's ask is closed as missed, as before
            log.error("Report from the day's messages failed: {}", type(exc).__name__)
    if built:  # counts only — never who or what
        await log_action(agent=AGENT, action="reports_built_from_chat", target_system="postgres", status="success",
                         mode="write", run_id=run_id, payload={"day": day.isoformat(), "built": built})
    return built


def auto_built_text(name: str, report: str) -> str:
    from integrations.org_bot.tone import casual
    from integrations.telegram.bot import escape

    return (casual(f"{escape(name)}, ҳисобот тугмасини босмаган экансиз, бугун юборган хабарларингиздан ҳисоботингиз "
                   "тузилди, раҳмат каттакон", keep=[escape(name)])
            + f"\n\n<i>{escape(report)}</i>")


async def close_day(now: datetime | None = None) -> int:
    """Close today's open asks once it's 22:00 or later; tell each person. How many were closed.

    First the reports the day's own unconfirmed messages can fill (``build_from_chat``, 2026-10-22).
    """
    now = now or now_local()
    if now.hour < store.REPORT_CLOSE_HOUR:
        return 0
    points = max(0, settings.report_miss_penalty) if now.date() >= PENALTY_FROM else 0
    if settings.openrouter_api_key.get_secret_value():
        await build_from_chat(now.date())
    await store.expire_held_messages()  # untapped «ҳисобот / савол» questions close with the day
    rows = await store.close_missed_reports(now.date(), points)
    if not rows:
        return 0
    told = 0
    if points:
        run_id = uuid.uuid4()
        async with TelegramBot(agent=AGENT, run_id=run_id,
                               bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
            for row in rows:
                text = kpi.build_penalty_text(names.call_name(row), points, int(row["month_points"] or points))
                try:
                    await bot.send_message(text, chat_id=str(row["telegram_user_id"]))
                    told += 1
                except TelegramError as exc:
                    log.error("Could not tell someone about the missed report: {}", type(exc).__name__)
    # Counts only — never who.
    await log_action(agent=AGENT, action="reports_closed", target_system="postgres", status="success",
                     mode="write", payload={"day": now.date().isoformat(), "missed": len(rows), "points": points,
                                            "told": told})
    log.info("Closed {} missed report(s) for {}, {} told", len(rows), now.date(), told)
    return len(rows)
