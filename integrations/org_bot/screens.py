"""The bot's own screens, opened by asking in words (2026-11-19, the owner: "keep the important ones in the menu, the
others should be capabilities — when the Boss asks anything, it does it").

The Director's ☰ menu keeps seven commands (``integrations/telegram/menus.py``). Everything else the menu used to
list — and the menu commands too — can be asked in plain words: the router sends such a request to the analyst
(``roles`` agent ``bot_ekranlari``), and the analyst's ``open_screen`` tool queues the screen; after its answer the bot
sends exactly what the command sends (the same function), so nothing is duplicated.

Actions that change something (excuse a day, set a goal, stop / start the leads or the daily cards) open the card with
its buttons — the Director confirms with his own tap; the AI never changes anything itself. The typed commands keep
working for anyone who remembers them. Only the Director (role) can open these; each screen opened is logged by key.
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
NEEDS_PERSON = frozenset({"set_goal", "excused"})
MAX_PER_ANSWER = 3


def fallback_text() -> str:
    """What the one-source fallback (analyst down) knows: each screen and its command."""
    lines = ["The bot's own screens (when the AI can't open one, tell the Director the command):"]
    lines += [f"- {label} — {command}" for label, command in SCREENS.values()]
    return "\n".join(lines)


async def data() -> str:
    """``_fetch_agent_data('bot_ekranlari')``."""
    return fallback_text()


def _person(name: str, employees: list[dict[str, Any]]) -> dict[str, Any] | None:
    from integrations.org_bot import greetings
    from integrations.org_bot.roles import DIRECTOR_ROLE

    staff = [e for e in employees if e.get("role") != DIRECTOR_ROLE]
    return greetings.match(name, staff) if name.strip() else None


async def open_screen(director_id: int, key: str, person_name: str, run_id: uuid.UUID) -> str:
    """Send one screen to the Director — the same function its command runs. Returns an outcome word."""
    from integrations.garmin import stock_list
    from integrations.org_bot import (duties, duty_review, excused, kpi_flow, lead_tracker, old_stock, ops_manager,
                                      plan_facts, sap_entry, store)
    from integrations.org_bot.roles import DIRECTOR_ROLE

    me = await store.get_employee_by_telegram_id(director_id)
    if me is None or me.get("status") != "active" or me.get("role") != DIRECTOR_ROLE or key not in SCREENS:
        return "screen_refused"
    today = today_local()
    if key == "pulse":
        return await ops_manager._send_pulse_link(director_id, run_id)
    if key == "kpi":
        return await kpi_flow._show_kpi(me, run_id)
    if key == "report_calendar":
        return await ops_manager._send_report_calendar(director_id, run_id)
    if key == "goals":
        return await kpi_flow._list_goals(director_id, run_id, kpi_flow._team(me))
    if key == "rating_cards":
        if settings.kpi_mode != "full":
            await ops_manager._reply(director_id, run_id, "Ҳозир оддий KPI ишлатилади (ҳисоботлар 50 % + топшириқлар "
                                                          "50 %) — баҳолаш карталари керак эмас.")
            return "rating_simple_mode"
        sent = await kpi_flow.send_rating_cards(director_id, kpi_flow.kpi_score.score_period(today), run_id)
        return "kpi_rating_cards" if sent else "kpi_nobody_to_rate"
    if key in NEEDS_PERSON:
        person = _person(person_name or "", await store.list_active_employees())
        if key == "set_goal":
            if person is None:
                return await kpi_flow._pick_employee(director_id, run_id, kpi_flow._team(me))
            return await kpi_flow.handle_callback("kg", str(person["id"]), {"id": "", "from": {"id": director_id}},
                                                  run_id) or "kpi_goal_started"
        if person is None:
            text, keyboard = excused.people_view(await excused._people(me))
        else:
            window = excused.window(today)
            days = await store.excused_days_of(str(person["id"]), window[0], window[-1])
            text, keyboard = excused.days_view(person, days, today)
        await ops_manager._reply(director_id, run_id, text, keyboard)
        return "excused_shown"
    if key == "leads":
        return await lead_tracker.send_director(director_id, run_id)
    if key == "reconcile":
        return await ops_manager._start_reconcile(me, run_id, None)
    if key == "duties":
        text, keyboard = await duties.director_card()
        await ops_manager._reply(director_id, run_id, text, keyboard)
        return "duties_register_sent"
    if key == "plans":
        await ops_manager._reply(director_id, run_id, plan_facts.plans_text(await plan_facts.plans_vs_facts(), today))
        return "plans_sent"
    if key == "sap_entry":
        await ops_manager._reply(director_id, run_id, await sap_entry.build(None, today))
        return "sap_entry_sent"
    if key == "weekly_review":
        await ops_manager._reply(director_id, run_id, "Ҳафта таҳлили тайёрланмоқда, бир дақиқа кутинг.")
        await duty_review.run(today, run_id, director_id)
        return "review_sent"
    if key == "garmin_stock":
        return await stock_list.send(director_id, run_id)
    return await old_stock.send(director_id, None, run_id)  # old_stock


async def open_all(director_id: int, wanted: list[tuple[str, str]], run_id: uuid.UUID) -> int:
    """The screens the analyst queued, after its answer — each once, at most ``MAX_PER_ANSWER``; one that fails is
    told, the rest still go. Returns how many opened."""
    from integrations.org_bot import ops_manager

    opened, seen = 0, set()
    for key, person in wanted:
        if (key, person) in seen or len(seen) >= MAX_PER_ANSWER:  # at most three TRIED — a failure brings no extra
            continue
        seen.add((key, person))
        try:
            outcome = await open_screen(director_id, key, person, run_id)
            opened += outcome != "screen_refused"
        except Exception as exc:  # noqa: BLE001 — one screen's failure never stops the answer or the others
            log.error("Screen {} failed: {}", key, type(exc).__name__)
            outcome = "screen_failed"
            try:
                await ops_manager._reply(director_id, run_id, f"Буни очиб бўлмади — {SCREENS[key][1]} буйруғини "
                                                              "ёзиб кўринг.")
            except Exception:  # noqa: BLE001
                pass
        await log_action(agent=AGENT, action="screen_opened", target_system="telegram", status="success",
                         run_id=run_id, mode="notify", payload={"screen": key, "outcome": outcome})
    return opened
