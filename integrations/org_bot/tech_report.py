"""IT's technical report — systems, connections, deliveries, errors, security; no figures.

The Director's order of 07.10.2026 («Маълумотларга кириш ҳуқуқларини
чеклаш», point 2): IT is taken off the CEO brief and gets its own report
instead — whether the systems work, connection errors, data updates, report
delivery, backups and security — with no money amounts and no confidential
details. Every line here is a time, a count or ok/failed: never an amount,
a customer, an employee's attendance or a message's text.

Sent to the admin in Admin Bot every morning after the 08:00 agents
(``agents/tech-report/agent.py``) and on demand with ``/texnik``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.db import fetch_read_only
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local, to_local
from integrations.telegram.bot import escape

log = setup_logging("tech-report")

SAP_SILENT_HOURS = 3  # the push runs every 30 min; the brief's own rule
BACKUP_LATE_HOURS = 26  # the office computer fetches one a day (scripts/backup/), with a little slack
LOW_AI_CREDIT = 3.0   # dollars left on the OpenRouter key before it's flagged

# agent_actions.agent -> how IT reads it
AGENT_LABELS = {
    "ceo-daily-brief": "Эрталабки брифинг",
    "daily-reports": "Кунлик ҳисобот сўрови",
    "team-cheer": "Кайфият хабарлари",
    "lead-agent": "Лид агенти",
    "receivables": "Дебиторлик огоҳлантириши",
    "task-tracker": "Топшириқлар назорати",
    "data-quality": "Маълумот сифати",
    "cash-calendar": "Пул календари",
    "billz-sap-check": "Billz → SAP текшируви",
    "sap-gateway-push": "SAP юбориши",
    "ops-manager-bot": "OPS Manager Bot",
    "admin-bot": "Admin Bot",
    "followup-drain": "Қўшимча савол навбати",
    "backup": "Захира нусха",
}

# SAP kinds as IT reads them in the report.
SAP_KIND_LABELS = {
    "ar_open": "очиқ ҳисоб-фактуралар", "sales": "сотув ҳужжатлари", "sales_lines": "сотув қаторлари",
    "stock_value": "омбор қиймати", "inventory": "омбор қолдиғи", "orders": "буюртмалар", "payments": "тўловлар",
    "customers": "мижозлар", "products": "маҳсулотлар", "warehouses": "омборлар",
}


@dataclass
class Check:
    name: str
    ok: bool | None  # None = not set up
    note: str = ""


@dataclass
class TechReport:
    at: datetime
    connections: list[Check] = field(default_factory=list)
    sap_last: datetime | None = None
    sap_kinds: int = 0
    sap_missing: dict[str, int] = field(default_factory=dict)  # kind -> columns missing in its latest push
    brief: str = ""
    runs: list[tuple[str, int, int, datetime | None]] = field(default_factory=list)  # (agent, ok, failed, last)
    errors: list[tuple[str, str, int]] = field(default_factory=list)  # (agent, short reason, count)
    db_size_mb: float | None = None
    backup_set_up: bool = False
    backup_last: datetime | None = None  # the last encrypted copy the office computer fetched
    backup_mb: float | None = None
    backup_failed: int = 0  # failed backups in 24 h
    analyst: tuple[int, int] = (0, 0)  # Director questions answered by the analyst / fallen back
    pending_access: int = 0
    data_access: int = 0  # people given company data through the bot (count only)
    refused: int = 0  # Admin Bot commands from someone else + closed /db tables asked for


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


async def _probe(name: str, configured: bool, call) -> Check:
    """One connection: configured? answers? (only a count or ok comes back, never data)."""
    if not configured:
        return Check(name, None, "уланмаган")
    try:
        return Check(name, True, await call())
    except Exception as exc:  # noqa: BLE001 — the reason is the point of this report
        message = str(exc).strip()
        return Check(name, False, (f"{type(exc).__name__}: {message}" if message else type(exc).__name__)[:160])


async def _onec() -> str:
    from integrations.onec.client import OneCClient

    async with OneCClient(agent="tech-report") as client:
        return f"{len(await client.entity_sets())} та объект очиқ"


async def _billz() -> str:
    from integrations.billz.client import BillzClient

    async with BillzClient(agent="tech-report") as client:
        return f"{len(await client.shops())} та дўкон"


async def _verifix() -> str:
    from integrations.verifix.client import VerifixClient

    async with VerifixClient(agent="tech-report") as client:
        await client.organisation()
    return "жавоб берди"


async def _openrouter() -> str:
    from integrations.ai.openrouter_client import describe_openrouter_key

    line = await describe_openrouter_key()
    if "failed" in line or "errored" in line or "empty" in line:
        raise RuntimeError(line.split(": ", 1)[-1][:120])
    try:
        left = float(line.split("$", 1)[1].split(" ", 1)[0])
    except (IndexError, ValueError):
        return "калит ишлаяпти"
    return "калит ишлаяпти" + (" — ⚠️ кредит кам қолди, тўлдиринг" if left < LOW_AI_CREDIT else "")


async def gather(now: datetime | None = None) -> TechReport:
    """Everything the report says — times, counts and ok/failed only."""
    now = now or now_local()
    since = now - timedelta(hours=24)
    report = TechReport(at=now)
    report.connections = [
        await _probe("1C", settings.onec_configured, _onec),
        await _probe("Billz", settings.billz_configured, _billz),
        await _probe("Verifix", settings.verifix_configured, _verifix),
        await _probe("AI (OpenRouter)", bool(settings.openrouter_api_key.get_secret_value().strip()), _openrouter),
    ]

    pushes = await fetch_read_only(
        """SELECT DISTINCT ON (action) action, occurred_at, payload FROM agent_actions
           WHERE agent = 'sap-gateway-push' AND status = 'success' ORDER BY action, occurred_at DESC"""
    )
    if pushes:
        report.sap_last = max(p["occurred_at"] for p in pushes)
        recent = [p for p in pushes if p["occurred_at"] >= report.sap_last - timedelta(hours=1)]
        report.sap_kinds = len(recent)
        for p in recent:
            missing = _payload(p["payload"]).get("missing_columns") or []
            if missing:
                kind = "ar_open" if p["action"] == "ar_aging_push" else p["action"].removeprefix("gateway_push_")
                report.sap_missing[kind] = len(missing)

    brief = await fetch_read_only(
        "SELECT status, generated_at, sent_at FROM daily_briefs ORDER BY generated_at DESC LIMIT 1"
    )
    if brief:
        b = brief[0]
        when = to_local(b["sent_at"] or b["generated_at"])
        report.brief = (f"юборилди {when:%d.%m %H:%M}" if b["status"] == "sent"
                        else f"{b['status']} ({when:%d.%m %H:%M})")

    for row in await fetch_read_only(
        """SELECT agent, count(*) FILTER (WHERE status = 'success') AS ok,
                  count(*) FILTER (WHERE status = 'failure') AS failed, max(occurred_at) AS last
           FROM agent_actions WHERE occurred_at >= %s GROUP BY agent ORDER BY agent""",
        (since,),
    ):
        report.runs.append((row["agent"], int(row["ok"]), int(row["failed"]), row["last"]))
    for row in await fetch_read_only(
        """SELECT agent, left(coalesce(error_message, action), 90) AS reason, count(*) AS n
           FROM agent_actions WHERE occurred_at >= %s AND status = 'failure'
           GROUP BY agent, reason ORDER BY n DESC LIMIT 8""",
        (since,),
    ):
        report.errors.append((row["agent"], row["reason"], int(row["n"])))

    from integrations.backup import dump as backup_dump

    report.backup_set_up = backup_dump.configured()
    fetched = await fetch_read_only(
        """SELECT occurred_at, payload FROM agent_actions WHERE agent = 'backup' AND action = 'backup_fetched'
           AND status = 'success' ORDER BY occurred_at DESC LIMIT 1"""
    )
    if fetched:
        report.backup_last = fetched[0]["occurred_at"]
        size_bytes = _payload(fetched[0]["payload"]).get("bytes")
        report.backup_mb = round(size_bytes / 1024 / 1024, 1) if isinstance(size_bytes, (int, float)) else None
    failed = await fetch_read_only(
        """SELECT count(*) AS n FROM agent_actions WHERE agent = 'backup' AND status = 'failure' AND occurred_at >= %s""",
        (since,),
    )
    report.backup_failed = int(failed[0]["n"]) if failed else 0

    size = await fetch_read_only("SELECT pg_database_size(current_database()) AS bytes")
    report.db_size_mb = round(size[0]["bytes"] / 1024 / 1024, 1) if size else None
    answered = await fetch_read_only(
        """SELECT count(*) FILTER (WHERE action = 'analyst_answer') AS answered,
                  count(*) FILTER (WHERE action = 'analyst_fallback') AS fallback
           FROM agent_actions WHERE agent = 'ops-manager-bot' AND occurred_at >= %s""",
        (since,),
    )
    if answered:
        report.analyst = (int(answered[0]["answered"]), int(answered[0]["fallback"]))
    granted = await fetch_read_only(
        """SELECT count(*) AS n FROM employees WHERE status = 'active' AND role IN ('buxgalteriya', 'moliya')
           AND cardinality(data_access) > 0"""
    )
    report.data_access = int(granted[0]["n"]) if granted else 0
    pending = await fetch_read_only("SELECT count(*) AS n FROM access_requests WHERE status = 'pending'")
    report.pending_access = int(pending[0]["n"]) if pending else 0
    refused = await fetch_read_only(
        """SELECT count(*) AS n FROM agent_actions WHERE occurred_at >= %s
           AND action IN ('unauthorized_admin_command', 'unauthorized_admin_callback', 'db_viewer_refused')""",
        (since,),
    )
    report.refused = int(refused[0]["n"]) if refused else 0
    return report


def _mark(ok: bool | None) -> str:
    return "✅" if ok else "⚪️" if ok is None else "❌"


def _backup_line(r: TechReport) -> str:
    """The copy outside Render (IT-5): when the office computer last fetched one."""
    if not r.backup_set_up:
        return "⚪️ Офисдаги нусха: созланмаган — Render'да иккита созлама керак (README, «Захира нусха»)"
    failed = f"; 24 соатда {r.backup_failed} та хато" if r.backup_failed else ""
    if r.backup_last is None:
        return "❌ Офисдаги нусха: ҳали бирорта ҳам олинмаган — офис компьютеридаги вазифани текширинг" + failed
    hours = (r.at - to_local(r.backup_last)).total_seconds() / 3600
    size = f", {r.backup_mb} MB" if r.backup_mb is not None else ""
    if hours > BACKUP_LATE_HOURS:
        return (f"❌ Офисдаги нусха: охиргиси {to_local(r.backup_last):%d.%m %H:%M}{size} — {int(hours)} соатдан бери "
                "олинмаган, офис компьютерини текширинг" + failed)
    return f"✅ Офисдаги нусха: {to_local(r.backup_last):%d.%m %H:%M}{size}" + failed


def render(r: TechReport) -> str:
    """The Admin Bot message."""
    lines = [f"🛠 <b>Техник ҳисобот — {r.at:%d.%m.%Y %H:%M}</b>", "", "<b>Уланишлар</b>"]
    if r.sap_last is None:
        lines.append("❌ SAP: ҳеч қачон маълумот юбормаган")
    else:
        hours = (r.at - to_local(r.sap_last)).total_seconds() / 3600
        ok = hours <= SAP_SILENT_HOURS
        lines.append(f"{_mark(ok)} SAP: охирги юбориш {to_local(r.sap_last):%d.%m %H:%M}"
                     + ("" if ok else f" — {int(hours)} соатдан бери келмаяпти, шлюз компьютерини текширинг")
                     + f"; турлар: {r.sap_kinds} та")
        if r.sap_missing:
            lines.append("   устунлари етишмайди: " + ", ".join(
                f"{SAP_KIND_LABELS.get(k, k)} ({n})" for k, n in sorted(r.sap_missing.items())))
    for c in r.connections:
        lines.append(f"{_mark(c.ok)} {c.name}: {escape(c.note)}")
    lines.append("⚪️ Didox: уланмаган")

    lines += ["", "<b>Ҳисоботлар етказилиши</b>",
              f"{_mark(r.brief.startswith('юборилди') if r.brief else False)} Эрталабки брифинг: {escape(r.brief or 'ҳали тузилмаган')}"]
    if r.analyst != (0, 0):
        lines.append(f"🤖 Директор саволлари (24 соат): AI жавоб берди {r.analyst[0]} та"
                     + (f", эски йўл билан {r.analyst[1]} та" if r.analyst[1] else ""))

    lines += ["", "<b>Ишлар (24 соат)</b>"]
    if r.runs:
        for agent, ok, failed, last in r.runs:
            label = AGENT_LABELS.get(agent, agent)
            when = f", охиргиси {to_local(last):%H:%M}" if last else ""
            lines.append(f"{'❌' if failed else '✅'} {escape(label)}: {ok} та амал" + (f", {failed} та хато" if failed else "") + when)
    else:
        lines.append("⚠️ 24 соатда ҳеч қандай иш қайд этилмаган")
    if r.errors:
        lines += ["", "<b>Хатолар</b>"]
        lines += [f"• {escape(AGENT_LABELS.get(a, a))}: {escape(reason)} ×{n}" for a, reason, n in r.errors]

    lines += ["", "<b>Захира нусха ва база</b>",
              f"База ҳажми: {r.db_size_mb} MB" if r.db_size_mb is not None else "База ҳажми: ўқилмади",
              _backup_line(r),
              "Render'нинг ўз нусхаси (3–7 кун): Render → mgmg-db → Recovery."]
    lines += ["", "<b>Хавфсизлик</b>",
              f"{'⚠️' if r.refused else '✅'} Рад этилган уринишлар (Admin Bot, ёпиқ жадваллар): {r.refused} та",
              f"Кутилаётган кириш сўровлари: {r.pending_access} та",
              f"Бот орқали маълумотга кириши бор ходимлар: {r.data_access} киши",
              "", "<i>Суммалар, мижозлар ва ходимларнинг маълумотлари бу ҳисоботда йўқ (Директор кўрсатмаси, 07.10.2026).</i>"]
    return "\n".join(lines)


async def send(run_id: uuid.UUID) -> str:
    """Gather, render and send to the admin chat."""
    from integrations.telegram.bot import TelegramBot, TelegramError

    if not settings.admin_bot_telegram_chat_id:
        log.warning("ADMIN_BOT_TELEGRAM_CHAT_ID is not set — nowhere to send the technical report")
        return "no_admin_chat"
    text = render(await gather())
    if settings.dry_run:
        print(text)
        return "dry_run"
    async with TelegramBot(
        agent="tech-report", run_id=run_id, bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        try:
            await bot.send_message(text)
        except TelegramError as exc:
            log.error("Could not send the technical report: {}", exc)
            return "failed"
    return "sent"
