"""Leads to two Telegram groups — Garmin and Primus, one message per lead (2026-10-26, the owner).

"Leads need to go to two separate groups in Telegram with their details, for Garmin and for Primus, one by one —
not in one message. I will add OPS Manager to the group."

  * **Binding:** add OPS Manager Bot to the group, then the admin or the Director types there
    ``/lidlar_garmin`` or ``/lidlar_primus`` (``/lidlar_stop`` unbinds). The bot answers in the group. Commands
    reach a bot even in privacy mode, so nothing else in the group needs to be read.
  * **Everything else in a group is ignored** — a group is never a chat with the bot: no tasks, reports, AI
    answers or join requests come from it.
  * **Garmin** — every new lead the Garmin AI bot posts (/webhooks/garmin-lead), and a phone number added later.
  * **Primus** — every new lead the Lead Agent adds to the sheet (08:00).
  * One message per lead, ``GAP_SECONDS`` apart (Telegram allows a group about 20 messages a minute). No group
    bound → nothing sent (logged). Counts-only logs. Phones go only to this internal group, never to the AI.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from integrations.common.config import settings
from integrations.common.db import execute, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import to_local
from integrations.telegram.bot import TelegramBot, escape

AGENT = "lead-groups"
log = setup_logging(AGENT)

BUSINESSES = {"garmin": "Garmin", "primus": "Primus"}
# Primus leads to a group: stopped 2026-10-26 (the owner: "stop sending leads to B2B Sotuv") — Garmin only.
BIND = {"/lidlar_garmin": "garmin"}
UNBIND = "/lidlar_stop"
GAP_SECONDS = 3.1
URGENCY = {"now": "🔥 Иссиқ — ҳозир олмоқчи", "next": "🟡 Илиқ — кейинроқ", "outage": "⚠️ Бот жавоб беролмади — мижоз кутмоқда"}
TRACKS = {"equipment_sales": "🏗 Ускуналар сотуви", "service_maintenance": "🔧 Сервис ва техник хизмат",
          "sponsorship_partnership": "🤝 Ҳомийлик / ҳамкорлик"}


def command(text: str | None) -> str:
    """«/lidlar_garmin@MgmgOpsBot extra» → «/lidlar_garmin»."""
    word = (text or "").strip().split(maxsplit=1)[0:1]
    return word[0].split("@")[0].lower() if word else ""


def _line(label: str, value: Any) -> str | None:
    value = " ".join(str(value or "").split())
    return f"{label}: {escape(value)}" if value else None


def _som(value: Any) -> str:
    try:
        return f"{float(value):,.0f}".replace(",", " ") + " сўм"
    except (TypeError, ValueError):
        return ""


def garmin_text(lead: dict[str, Any], phone_only: bool = False) -> str:
    """One Garmin lead (garmin_leads row or the cleaned webhook event) as one group message."""
    who = " ".join(x for x in (lead.get("name") or "", f"@{lead['username']}" if lead.get("username") else "") if x)
    at = lead.get("created_at") or lead.get("at")
    if phone_only:
        head = "📞 <b>Garmin лид — телефон қўшилди</b>"
    else:
        head = f"<b>{URGENCY.get(lead.get('urgency') or '', 'Garmin лид')}</b> · Garmin лид"
    product = " — ".join(x for x in (escape(lead.get("product_name") or ""), _som(lead.get("price"))) if x)
    lines = [head, _line("Мижоз", who or "исмсиз"), _line("Телефон", lead.get("phone") or "кўрсатилмаган"),
             f"Товар: {product}" if product else None, _line("Бюджет", lead.get("budget")),
             _line("Изоҳ", (lead.get("summary") or "")[:800]),
             _line("Манба", " · ".join(x for x in (lead.get("source") or "", f"{to_local(at):%d.%m %H:%M}" if at else "") if x))]
    return "\n".join(x for x in lines if x)


def primus_text(lead: dict[str, Any]) -> str:
    """One Lead Agent lead (a qualified, new one) as one group message."""
    name = " — ".join(x for x in (lead.get("company_name") or "Номсиз", lead.get("project_name") or "") if x)
    contact = ", ".join(x for x in (lead.get("contact_name") or "", lead.get("contact_role") or "",
                                    lead.get("contact_method") or "") if x)
    lines = [f"<b>{TRACKS.get(lead.get('track') or '', '💼 Лид')}</b> · Primus лид", f"<b>{escape(name)}</b>",
             _line("Соҳа", lead.get("industry")), _line("Жой", lead.get("location")),
             _line("Босқич", lead.get("project_stage")), _line("Очилиши", lead.get("estimated_opening")),
             _line("Ҳажми", lead.get("estimated_size")),
             _line("Сигнал", " ".join(x for x in ((lead.get("signal") or "")[:700], f"({lead['signal_date']})"
                                                     if lead.get("signal_date") else "") if x)),
             _line("Манба", lead.get("signal_source_url")), _line("Алоқа", contact),
             _line("Устуворлик", " · ".join(x for x in (str(lead.get("priority") or ""),
                                                        f"ишонч {lead['confidence']}" if lead.get("confidence") else "") if x)),
             _line("Изоҳ", (lead.get("notes") or "")[:500])]
    return "\n".join(x for x in lines if x)


# ---------------------------------------------------------------------------------------------------- the groups

async def group_of(business: str) -> int | None:
    row = await fetch_one("SELECT chat_id FROM lead_groups WHERE business = %s", (business,))
    return int(row["chat_id"]) if row else None


def _bot(run_id: uuid.UUID | None) -> TelegramBot:
    return TelegramBot(agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def handle_group_message(message: dict[str, Any], run_id: uuid.UUID) -> str:
    """A message in a group: only the admin's / the Director's binding commands count; the rest is ignored."""
    word = command(message.get("text"))
    if word not in BIND and word != UNBIND:
        return "group_ignored"
    sender = (message.get("from") or {}).get("id")
    if not await _may_bind(sender):
        return "group_ignored"
    chat = message.get("chat") or {}
    if word == UNBIND:
        await execute("DELETE FROM lead_groups WHERE chat_id = %s", (chat.get("id"),))
        reply = "Бу гуруҳга лидлар энди юборилмайди."
    else:
        business = BIND[word]
        await execute(
            """INSERT INTO lead_groups (business, chat_id, title, set_by) VALUES (%s, %s, %s, %s)
               ON CONFLICT (business) DO UPDATE SET chat_id = EXCLUDED.chat_id, title = EXCLUDED.title,
                   set_by = EXCLUDED.set_by, set_at = now()""",
            (business, chat.get("id"), chat.get("title"), sender))
        reply = (f"✅ Бу гуруҳга {BUSINESSES[business]} лидлари юборилади — ҳар бир лид алоҳида хабар бўлиб. "
                 "Тўхтатиш: /lidlar_stop")
    async with _bot(run_id) as bot:
        await bot.send_message(reply, chat_id=str(chat.get("id")))
    await log_action(agent=AGENT, action="lead_group_set" if word != UNBIND else "lead_group_cleared",
                     target_system="postgres", status="success", run_id=run_id, mode="write",
                     payload={"business": BIND.get(word), "by": sender})
    return "lead_group_set" if word != UNBIND else "lead_group_cleared"


async def _may_bind(telegram_user_id: int | None) -> bool:
    """The admin (Admin Bot's admin id) or an active Director."""
    if not telegram_user_id:
        return False
    if settings.admin_bot_admin_user_id and telegram_user_id == settings.admin_bot_admin_user_id:
        return True
    from integrations.org_bot import store
    from integrations.org_bot.roles import DIRECTOR_ROLE

    employee = await store.get_employee_by_telegram_id(telegram_user_id)
    return bool(employee and employee.get("status") == "active" and employee.get("role") == DIRECTOR_ROLE)


async def send(business: str, texts: list[str], run_id: uuid.UUID | None = None) -> int:
    """Each text as its own message to the business's group, one after another; how many went."""
    if not texts or settings.bots_frozen:
        return 0
    try:
        chat_id = await group_of(business)
    except Exception as exc:  # noqa: BLE001 — leads are stored anyway; only the group message is missed
        log.error("Lead group not read: {}", type(exc).__name__)
        return 0
    if chat_id is None:
        log.info("No {} lead group set — {} lead(s) not posted", business, len(texts))
        return 0
    sent = 0
    async with _bot(run_id) as bot:
        for i, text in enumerate(texts):
            if i:
                await asyncio.sleep(GAP_SECONDS)
            try:
                await bot.send_message(text, chat_id=str(chat_id))
                sent += 1
            except Exception as exc:  # noqa: BLE001 — the next lead still goes
                log.error("Lead not posted to the {} group: {}", business, type(exc).__name__)
    await log_action(agent=AGENT, action="leads_posted", target_system="telegram", status="success", run_id=run_id,
                     mode="notify", payload={"business": business, "leads": len(texts), "sent": sent})
    return sent
