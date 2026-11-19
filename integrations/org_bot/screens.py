"""The bot's own screens, opened by asking in words (2026-11-19, the owner: "keep the important ones in the menu, the
others should be capabilities — when the Boss asks anything, it does it").

The Director's ☰ menu is PULSE only (``integrations/telegram/menus.py``; seven commands earlier on 19.11), and the
Garmin manager's the same. Everything else either menu used to list can be asked in plain words: the Director's
request goes through the router to the analyst (``roles`` agent ``bot_ekranlari``), the Garmin manager's straight to
his analyst; the ``open_screen`` tool queues the screen, and after the answer the bot sends exactly what the command
sends (the same function), so nothing is duplicated. Each role has its own list — the Garmin manager's screens are
his team's, as his commands are (``GARMIN_SCREENS``).

Actions that change something (excuse a day, set a goal, stop / start the leads or the daily cards) open the card with
its buttons — the person confirms with their own tap; the AI never changes anything itself. The typed commands keep
working for anyone who remembers them. Each screen opened is logged by key.
"""

from __future__ import annotations

import uuid
from typing import Any

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local

AGENT = "ops-manager-bot"
log = setup_logging("screens")

# key → (what it is, the command that also opens it). The analyst's tool enum and its prompt come from here.
SCREENS: dict[str, tuple[str, str]] = {
    "pulse": ("PULSE — компания ва ходимларнинг жонли графиги (ҳавола)", "/pulse"),
    "kpi": ("ходимлар KPI жадвали", "/kpi"),
    "report_calendar": ("ҳисоботлар календари — ким юборди, ким юбормади (расм)", "/hisobotlar"),
    "goals": ("ойлик мақсадлар ва натижалар рўйхати", "/maqsadlar"),
    "set_goal": ("ходимга ойлик мақсад қўйиш (ходим танланади ёки исми айтилган бўлса — шу ходим)", "/maqsad"),
    "rating_cards": ("ойлик баҳолаш карталари (тўлиқ KPI режимида)", "/baho"),
    "excused": ("сабабли кун белгилаш — ходимнинг 14 кунлик жадвали, кунни босиб кулранг қилинади", "/sababli"),
    "leads": ("B2B лидлар статистикаси, ⏸ тўхтатиш / ▶️ бошлаш тугмаси", "/lidlar"),
    "reconcile": ("SAP ↔ 1C ↔ Billz солиштириш — фарқлар ва хатолар (Excel)", "/farqlar"),
    "duties": ("вазифалар реестри: ким нима қилади, ойлик режалар; кунлик карталар ▶️ / ⏸", "/vazifalar"),
    "plans": ("ойлик режа ва факт (SAP)", "/reja"),
    "sap_entry": ("SAP'га ким киритмади: Billz чеклари, кеч киритилган счётлар", "/kiritish"),
    "weekly_review": ("ҳафталик вазифалар таҳлили — ҳозир", "/tahlil"),
    "garmin_stock": ("Garmin қолдиғи: ҳар товар ва қанча қолгани (Excel)", "/qoldiq"),
    "old_stock": ("бир йилга яқинлашган ва ошган товарлар (PDF)", "/eski"),
}
# The Garmin manager's (2026-11-19, his menu trimmed the same way): his team's, as each of his commands is.
GARMIN_SCREENS: dict[str, tuple[str, str]] = {
    "pulse": ("PULSE — жамоанинг жонли графиги (ҳавола)", "/pulse"),
    "kpi": ("Garmin сотув жамоаси KPI жадвали", "/kpi"),
    "report_calendar": ("жамоа ҳисоботлари календари — ким юборди, ким юбормади (расм)", "/hisobotlar"),
    "goals": ("жамоа мақсадлари ва натижалари — натижани киритиш тугмалари билан", "/maqsadlar"),
    "set_goal": ("жамоадаги ходимга ойлик мақсад қўйиш (исми айтилган бўлса — шу ходим)", "/maqsad"),
    "excused": ("жамоадаги ходимнинг сабабли кунини белгилаш — 14 кунлик жадвал", "/sababli"),
    "duties": ("жамоанинг бугунги куни: кунлик вазифа карталари", "/vazifalar"),
    "sap_entry": ("жамоа: SAP'га ким киритмади — Billz чеклари, кеч киритилган счётлар", "/kiritish"),
    "garmin_stock": ("Garmin қолдиғи: ҳар товар ва қанча қолгани (Excel)", "/qoldiq"),
    "old_stock": ("Garmin омборларида бир йилга яқинлашган ва ошган товарлар (PDF)", "/eski"),
    "competitors": ("рақобатчилар: Garmin'ни бошқалар қанчага сотмоқда (OLX, дўконлар)", "/raqobat"),
}
NEEDS_PERSON = frozenset({"set_goal", "excused"})
MAX_PER_ANSWER = 3


def for_role(role: str | None) -> dict[str, tuple[str, str]]:
    """The screens a role may open: the Director's, the Garmin manager's (his team's), none for anyone else."""
    from integrations.org_bot.roles import DIRECTOR_ROLE

    return SCREENS if role == DIRECTOR_ROLE else GARMIN_SCREENS if role == "garmin_manager" else {}


def fallback_text() -> str:
    """What the one-source fallback (analyst down) knows: each screen and its command."""
    lines = ["The bot's own screens (when the AI can't open one, tell the Director the command):"]
    lines += [f"- {label} — {command}" for label, command in SCREENS.values()]
    return "\n".join(lines)


async def data() -> str:
    """``_fetch_agent_data('bot_ekranlari')``."""
    return fallback_text()


def _person(name: str, people: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one person of ``people`` (those the command's own picker lists) the name means."""
    from integrations.org_bot import greetings

    return greetings.match(name, people) if name.strip() else None


async def open_screen(asker_id: int, key: str, person_name: str, run_id: uuid.UUID) -> str:
    """Send one screen to the Director or the Garmin manager — the same function the command runs for that role.
    Returns an outcome word."""
    from integrations.garmin import stock_list
    from integrations.org_bot import (duties, duty_review, excused, kpi_flow, lead_tracker, old_stock, ops_manager,
                                      plan_facts, report_calendar, rollout, sap_entry, store)

    me = await store.get_employee_by_telegram_id(asker_id)
    if me is None or me.get("status") != "active" or key not in for_role(me.get("role")):
        return "screen_refused"
    today = today_local()
    team = report_calendar.TEAM_OF.get(me.get("role") or "")  # None = the Director: everyone
    if key == "pulse":
        return await ops_manager._send_pulse_link(asker_id, run_id)
    if key == "kpi":
        return await kpi_flow._show_kpi(me, run_id)
    if key == "report_calendar":
        return await ops_manager._send_report_calendar(asker_id, run_id, team)
    if key == "goals":
        return await kpi_flow._list_goals(asker_id, run_id, kpi_flow._team(me))
    if key == "rating_cards":
        if settings.kpi_mode != "full":
            await ops_manager._reply(asker_id, run_id, "Ҳозир оддий KPI ишлатилади (ҳисоботлар 50 % + топшириқлар "
                                                       "50 %) — баҳолаш карталари керак эмас.")
            return "rating_simple_mode"
        sent = await kpi_flow.send_rating_cards(asker_id, kpi_flow.kpi_score.score_period(today), run_id)
        return "kpi_rating_cards" if sent else "kpi_nobody_to_rate"
    if key == "set_goal":
        person = _person(person_name or "", await kpi_flow._people(kpi_flow._team(me)))
        if person is None:
            return await kpi_flow._pick_employee(asker_id, run_id, kpi_flow._team(me))
        return await kpi_flow.handle_callback("kg", str(person["id"]), {"id": "", "from": {"id": asker_id}},
                                              run_id) or "kpi_goal_started"
    if key == "excused":
        people = await excused._people(me)  # the Director: everyone who reports; the Garmin manager: his team
        person = _person(person_name or "", people)
        if person is None:
            text, keyboard = excused.people_view(people)
        else:
            window = excused.window(today)
            days = await store.excused_days_of(str(person["id"]), window[0], window[-1])
            text, keyboard = excused.days_view(person, days, today)
        await ops_manager._reply(asker_id, run_id, text, keyboard)
        return "excused_shown"
    if key == "leads":
        return await lead_tracker.send_director(asker_id, run_id)
    if key == "reconcile":
        return await ops_manager._start_reconcile(me, run_id, None)
    if key == "duties":
        if team is not None:  # his team's day, as his /vazifalar
            await ops_manager._reply(asker_id, run_id, await rollout.team_view(me, today))
            return "duties_team_sent"
        text, keyboard = await duties.director_card()
        await ops_manager._reply(asker_id, run_id, text, keyboard)
        return "duties_register_sent"
    if key == "plans":
        await ops_manager._reply(asker_id, run_id, plan_facts.plans_text(await plan_facts.plans_vs_facts(), today))
        return "plans_sent"
    if key == "sap_entry":
        await ops_manager._reply(asker_id, run_id, await sap_entry.build(team, today))
        return "sap_entry_sent"
    if key == "weekly_review":
        await ops_manager._reply(asker_id, run_id, "Ҳафта таҳлили тайёрланмоқда, бир дақиқа кутинг.")
        await duty_review.run(today, run_id, asker_id)
        return "review_sent"
    if key == "garmin_stock":
        return await stock_list.send(asker_id, run_id)
    if key == "competitors":
        await ops_manager._reply(asker_id, run_id, "OLX ва бошқа сайтларни текширяпман, бир-икки дақиқа кутинг.")
        await ops_manager._send_competitors(asker_id, run_id)
        return "competitors_sent"
    # old_stock: the Director every warehouse, the Garmin manager the Garmin ones (as /eski)
    return await old_stock.send(asker_id, None if team is None else await old_stock.garmin_codes_now(), run_id)


async def open_all(asker_id: int, wanted: list[tuple[str, str]], run_id: uuid.UUID) -> int:
    """The screens the analyst queued, after its answer — each once, at most ``MAX_PER_ANSWER``; one that fails is
    told, the rest still go. Returns how many opened."""
    from integrations.org_bot import ops_manager

    opened, seen = 0, set()
    for key, person in wanted:
        if (key, person) in seen or len(seen) >= MAX_PER_ANSWER:  # at most three TRIED — a failure brings no extra
            continue
        seen.add((key, person))
        try:
            outcome = await open_screen(asker_id, key, person, run_id)
            opened += outcome != "screen_refused"
        except Exception as exc:  # noqa: BLE001 — one screen's failure never stops the answer or the others
            log.error("Screen {} failed: {}", key, type(exc).__name__)
            outcome = "screen_failed"
            command = (SCREENS.get(key) or GARMIN_SCREENS.get(key) or ("", ""))[1]
            try:
                await ops_manager._reply(asker_id, run_id, f"Буни очиб бўлмади — {command} буйруғини ёзиб кўринг."
                                         if command else "Буни очиб бўлмади, бир оздан кейин қайта сўранг.")
            except Exception:  # noqa: BLE001
                pass
        await log_action(agent=AGENT, action="screen_opened", target_system="telegram", status="success",
                         run_id=run_id, mode="notify", payload={"screen": key, "outcome": outcome})
    return opened
