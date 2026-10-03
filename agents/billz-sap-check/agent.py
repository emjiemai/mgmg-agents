"""Agent — Billz → SAP check: did every shop cheque reach SAP, on time?

From the owner, 2026-10-03: a watch sold in the Garmin shop is rung up in
BILLZ, and the shop enters the same sale into SAP at the end of the day.
When that is late or forgotten the books are wrong — it has cost the
business thousands of dollars — so the system compares the two every
morning instead of a person. The rules are in ``integrations/billz/sap_check.py``.

Runs at 08:00 with the morning agents, for yesterday (cheques of the last 14
days that never reached SAP are repeated until they do). The Director gets
one message: a ✅ line when everything matches, the list when not. Other
roles can be added with ``BILLZ_SAP_CHECK_ROLES``. While
``BILLZ_SAP_CHECK_TRIAL`` is on (the default until the first results are
confirmed), it goes to the admin in Admin Bot instead.

Needs both sides: BILLZ (``BILLZ_SECRET_TOKEN``) and SAP's invoice lines from
the gateway tool ``get_sales_by_date`` (``docs/sap-gateway-tools.md``; today's
capped tools carry no lines, warehouse or so'm total). Until that tool's data
has arrived once, this exits quietly.

Run:
    python agents/billz-sap-check/agent.py             # yesterday, once a day
    python agents/billz-sap-check/agent.py --dry-run   # print, send nothing, store nothing
    python agents/billz-sap-check/agent.py --force     # send again
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

# The folder name contains a hyphen, so this file cannot be imported as a
# package. Put the project root on sys.path and run it as a script instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.billz import sap_check
from integrations.billz.client import PAUSE, BillzClient
from integrations.common.config import settings
from integrations.common.db import close_pool, execute, fetch_all, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import TASHKENT, now_local, today_local, to_local
from integrations.org_bot import store
from integrations.org_bot.roles import DIRECTOR_ROLE
from integrations.telegram.bot import TelegramBot, TelegramError

AGENT = "billz-sap-check"
log = setup_logging(AGENT)

REQUIRED_SETTINGS = {"ops_manager_bot_telegram_bot_token"}

# SAP's data must have arrived within this long, or nothing is compared.
STALE_AFTER = timedelta(hours=24)


async def last_sales_push() -> datetime | None:
    """When the full push last delivered SAP's sales documents (Tashkent time), or None."""
    row = await fetch_one(
        "SELECT occurred_at, payload FROM agent_actions WHERE agent = 'sap-gateway-push' "
        "AND action = 'gateway_push_sales' AND status = 'success' ORDER BY occurred_at DESC LIMIT 1"
    )
    if row is None:
        return None
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
    if not payload.get("complete"):
        return None
    return to_local(row["occurred_at"])


async def _sap_rows(tool: str) -> list[dict[str, Any]]:
    return [
        r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"])
        for r in await fetch_all("SELECT raw FROM v_sap_gateway_latest WHERE tool = %s", (tool,))
    ]


async def billz_cheques(
    start: date, end: date, run_id: uuid.UUID
) -> tuple[list[sap_check.Cheque], dict[date, int], list[str]]:
    """Every cheque of every shop, day by day.

    Returns:
        ``(cheques, so'm on lines without a cheque id per day, shop_names)``.
    """
    cheques: list[sap_check.Cheque] = []
    unkeyed: dict[date, int] = {}
    async with BillzClient(agent=AGENT, run_id=run_id) as client:
        shops = [s for s in await client.shops() if s.get("id") and not s.get("deleted_at")]
        shop_ids = [str(s["id"]) for s in shops]
        day = start
        while day <= end and shop_ids:
            await asyncio.sleep(PAUSE)
            found, without_id = sap_check.cheques_from_billz(await client.positions(day, shop_ids), day)
            cheques += found
            if without_id:
                unkeyed[day] = without_id
            day += timedelta(days=1)
    return cheques, unkeyed, [str(s.get("name") or "") for s in shops]


def compare(
    cheques: list[sap_check.Cheque], sales: list[dict[str, Any]], lines: list[dict[str, Any]], day: date,
    mapping: dict[str, set[str]],
) -> sap_check.Result:
    """Check each group of shops against the SAP warehouses they sell from, then merge."""
    groups: dict[frozenset[str], list[sap_check.Cheque]] = defaultdict(list)
    for codes in mapping.values():
        groups.setdefault(frozenset(codes), [])
    for cheque in cheques:
        codes = sap_check.warehouses_for(cheque.shop, mapping)
        if codes:
            groups[codes].append(cheque)
        else:
            log.warning("Billz shop '{}' has no SAP warehouse in BILLZ_SAP_WAREHOUSES — not checked", cheque.shop)
    merged = sap_check.Result(day=day)
    for codes, group in groups.items():
        part = sap_check.check(group, sap_check.docs_from_sap(sales, lines, codes), day)
        merged.cheques_day += part.cheques_day
        merged.docs_day += part.docs_day
        merged.missing += part.missing
        merged.amount_diff += part.amount_diff
        merged.late += part.late
        merged.extra += part.extra
        merged.returned += part.returned
    return merged


def all_shop_docs(
    sales: list[dict[str, Any]], lines: list[dict[str, Any]], mapping: dict[str, set[str]]
) -> list[sap_check.Doc]:
    """SAP documents of every mapped warehouse, each once."""
    codes = frozenset(c for group in mapping.values() for c in group)
    return sap_check.docs_from_sap(sales, lines, codes)


async def _recipients() -> dict[int, dict[str, Any]]:
    roles = [DIRECTOR_ROLE] + [r.strip() for r in settings.billz_sap_check_roles.split(",") if r.strip()]
    return {e["telegram_user_id"]: e for role in roles for e in await store.active_employees_by_role(role)}


async def _already_sent(day: date) -> bool:
    row = await fetch_one("SELECT sent_at FROM billz_sap_checks WHERE check_date = %s", (day,))
    return bool(row and row.get("sent_at"))


async def _save(day: date, status: str, result: dict[str, Any], sent: bool) -> None:
    await execute(
        """
        INSERT INTO billz_sap_checks (check_date, status, result, sent_at)
        VALUES (%s, %s, %s::jsonb, CASE WHEN %s THEN now() END)
        ON CONFLICT (check_date) DO UPDATE SET
            status = EXCLUDED.status, result = EXCLUDED.result, checked_at = now(),
            sent_at = COALESCE(EXCLUDED.sent_at, billz_sap_checks.sent_at)
        """,
        (day, status, json.dumps(result, ensure_ascii=False, default=str), sent),
    )


def trial_text(text: str) -> str:
    """The admin's copy while the check is on trial."""
    return ("🧪 <i>Синов: Директорга ҳали юборилмайди. Тўғри бўлса, Render'да "
            "BILLZ_SAP_CHECK_TRIAL=false қилинг.</i>\n\n" + text)


async def _send(text: str, run_id: uuid.UUID) -> int:
    if settings.billz_sap_check_trial:
        async with TelegramBot(
            agent=AGENT, run_id=run_id, bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
            default_chat_id=settings.admin_bot_telegram_chat_id,
        ) as bot:
            try:
                await bot.send_message(trial_text(text))
                return 1
            except TelegramError as exc:
                log.error("Could not send the Billz–SAP trial to the admin: {}", exc)
                return 0
    sent = 0
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for telegram_user_id in await _recipients():
            try:
                await bot.send_message(text, chat_id=str(telegram_user_id))
                sent += 1
            except TelegramError as exc:
                log.error("Could not send the Billz–SAP check to {}: {}", telegram_user_id, exc)
    return sent


async def run(dry_run: bool = False, force: bool = False) -> int:
    """Check yesterday and tell the Director.

    Returns:
        Process exit code — 0 on success or nothing to do, 2 if config is incomplete.
    """
    if dry_run:
        settings.dry_run = True
    run_id = uuid.uuid4()
    if not settings.billz_sap_check_enabled and not settings.dry_run:
        log.info("Billz–SAP check is paused (BILLZ_SAP_CHECK_ENABLED=false)")
        return 0
    if not settings.billz_configured:
        log.info("BILLZ is not set up (BILLZ_SECRET_TOKEN) — nothing to check")
        return 0
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
        return 0
    unfilled = REQUIRED_SETTINGS & set(settings.missing_placeholders())
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(sorted(unfilled)))
        return 2

    today = today_local()
    day = today - timedelta(days=1)
    if await _already_sent(day) and not force and not settings.dry_run:
        log.info("The check for {} was already sent — skipping", day)
        return 0

    pushed_at = await last_sales_push()
    if pushed_at is None:
        # get_sales_by_date isn't in the gateway yet: SAP's invoice lines aren't here at all.
        log.info("No SAP invoice lines yet (gateway tool get_sales_by_date, docs/sap-gateway-tools.md) — nothing to compare")
        return 0
    if now_local() - pushed_at > STALE_AFTER:
        text = sap_check.render_stale(day, pushed_at)
        status, result = "stale_sap", {"pushed_at": str(pushed_at)}
    else:
        start = day - timedelta(days=sap_check.WINDOW_DAYS - 1)
        cheques, unkeyed, shops = await billz_cheques(start, day, run_id)
        mapping = sap_check.warehouse_map(settings.billz_sap_warehouses)
        sales, lines = await _sap_rows("sales"), await _sap_rows("sales_lines")
        if unkeyed:
            # BILLZ gave lines without cheque ids: no cheque can be named, so
            # only yesterday's totals are compared — never a false "missing".
            log.warning("BILLZ lines without cheque ids on {} — comparing totals only", sorted(unkeyed))
            billz_total = sum(c.amount for c in cheques if c.day == day) + unkeyed.get(day, 0)
            checked = sap_check.totals_only(day, billz_total, all_shop_docs(sales, lines, mapping))
        else:
            checked = compare(cheques, sales, lines, day, mapping)
        day_end = datetime.combine(today, time.min, tzinfo=TASHKENT)
        text = sap_check.render(checked, pushed_at=pushed_at, day_end=day_end)
        status, result = checked.status, {**sap_check.summary(checked), "shops": shops,
                                          "pushed_at": str(pushed_at)}

    if settings.dry_run:
        print(text)
        where = "the admin (trial)" if settings.billz_sap_check_trial else f"{len(await _recipients())} person(s)"
        log.info("[dry run] status={}, would go to {}", status, where)
        return 0

    sent = await _send(text, run_id)
    await _save(day, status, result, sent > 0)
    await log_action(
        agent=AGENT, action="checked", target_system="telegram", status="success", run_id=run_id,
        target_ref=str(day), mode="write", payload={"status": status, "sent": sent},
    )
    log.info("Billz–SAP check for {}: {} — sent to {} person(s)", day, status, sent)
    return 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Did every Billz cheque reach SAP? (yesterday)")
    parser.add_argument("--dry-run", action="store_true", help="print, send nothing, store nothing")
    parser.add_argument("--force", action="store_true", help="send even if already sent today")
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(_main(args.dry_run, args.force))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


async def _main(dry_run: bool, force: bool) -> int:
    try:
        return await run(dry_run, force)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
