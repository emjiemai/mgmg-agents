"""The database review — the bot's own tables, every morning (2026-10-17, the owner).

The owner opened /db and found the bot's records messy: messages held for
weeks, test rows, questions saved as reports, removed people still holding
work. This checks the bot's OWN tables only — never SAP, 1C, Billz or
Verifix data — and tells IT in Admin Bot, counts only (no names, no text:
the Director's order of 07.10.2026).

Two kinds of finding:
  * fixed here, safely — an UPDATE that closes something stale, never a
    DELETE (held messages nobody tapped; a removed person's open tasks);
  * reported — what needs a person's decision (test rows, duplicates…), with
    the /db table to look in.

08:00 in the morning job (after the brief); Admin Bot /baza runs it now.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from integrations.common.db import fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import store

AGENT = "db-review"
log = setup_logging(AGENT)
COMMANDS = ("/baza", "/база")

# Words a test row is made of (complaints, reports, tasks): the whole text is one of these.
_TEST = r"^\s*(test|тест|teст|sinov|синов|проверка|check|asd+|qwe+|1+|\.+|-+)\s*[.!]*\s*$"


@dataclass(frozen=True)
class Check:
    key: str
    label: str      # Uzbek Cyrillic, shown to IT
    where: str      # the /db table to look in
    count_sql: str  # one row, column n
    fix_sql: str | None = None  # a safe UPDATE that settles it; None = report only


CHECKS: tuple[Check, ...] = (
    Check("held_stale", "«ҳисобот / савол» жавобсиз қолган хабарлар (кечагача)", "pending_relays",
          "SELECT count(*) AS n FROM pending_relays WHERE resolved_at IS NULL "
          "AND created_at < (now() AT TIME ZONE 'Asia/Tashkent')::date AT TIME ZONE 'Asia/Tashkent'",
          "UPDATE pending_relays SET resolved_at = now(), outcome = 'expired' WHERE resolved_at IS NULL "
          "AND created_at < (now() AT TIME ZONE 'Asia/Tashkent')::date AT TIME ZONE 'Asia/Tashkent'"),
    Check("removed_open_tasks", "ўчирилган ходимларнинг очиқ топшириқлари", "tasks",
          "SELECT count(*) AS n FROM tasks t JOIN employees e ON e.id = t.assigned_employee_id "
          "WHERE e.status = 'revoked' AND t.status IN ('sent', 'started', 'blocked', 'review')",
          "UPDATE tasks t SET status = 'cancelled', awaiting = NULL, awaiting_since = NULL FROM employees e "
          "WHERE e.id = t.assigned_employee_id AND e.status = 'revoked' "
          "AND t.status IN ('sent', 'started', 'blocked', 'review')"),
    Check("old_role_open_tasks", "роли ўзгарган ходимда аввалги ролдаги очиқ топшириқлар", "tasks",
          "SELECT count(*) AS n FROM tasks t JOIN employees e ON e.id = t.assigned_employee_id "
          "WHERE e.status = 'active' AND t.target_role <> e.role AND t.status IN ('sent', 'started', 'blocked', 'review')",
          "UPDATE tasks t SET status = 'cancelled', awaiting = NULL, awaiting_since = NULL FROM employees e "
          "WHERE e.id = t.assigned_employee_id AND e.status = 'active' AND t.target_role <> e.role "
          "AND t.status IN ('sent', 'started', 'blocked', 'review')"),
    Check("duplicate_open_tasks", "бир ходимда бир хил очиқ топшириқ икки марта", "tasks",
          "SELECT count(*) AS n FROM (SELECT assigned_employee_id, lower(btrim(task_summary)) FROM tasks "
          "WHERE status IN ('sent', 'started', 'blocked', 'review') GROUP BY 1, 2 HAVING count(*) > 1) d"),
    Check("questions_as_reports", "ҳисобот сифатида сақланган саволлар («?» билан)", "daily_reports",
          "SELECT count(*) AS n FROM daily_reports WHERE status = 'submitted' AND content LIKE '%%?%%' "
          "AND length(content) < 200"),
    Check("empty_reports", "жуда қисқа ҳисоботлар (5 ҳарфгача)", "daily_reports",
          "SELECT count(*) AS n FROM daily_reports WHERE status = 'submitted' AND length(btrim(coalesce(content, ''))) < 5"),
    Check("test_complaints", "синов шикоятлари («test», «тест»…)", "client_feedback",
          f"SELECT count(*) AS n FROM client_feedback WHERE message ~* '{_TEST}'"),
    Check("test_tasks", "синов топшириқлари", "tasks",
          f"SELECT count(*) AS n FROM tasks WHERE raw_message ~* '{_TEST}' OR task_summary ~* '{_TEST}'"),
    Check("unnamed", "бир кундан бери исмини ёзмаган ходимлар", "employees",
          "SELECT count(*) AS n FROM employees WHERE status = 'active' AND coalesce(btrim(full_name), '') = '' "
          "AND created_at < now() - interval '1 day'"),
    Check("access_wrong_role", "маълумотга кириш рухсати ноўрин ролда", "employees",
          "SELECT count(*) AS n FROM employees WHERE cardinality(data_access) > 0 "
          "AND (status <> 'active' OR role NOT IN ('buxgalteriya', 'moliya'))",
          "UPDATE employees SET data_access = '{}' WHERE cardinality(data_access) > 0 "
          "AND (status <> 'active' OR role NOT IN ('buxgalteriya', 'moliya'))"),
    Check("files_stale", "жавобсиз қолган файллар (2 кундан ортиқ)", "employee_files",
          "SELECT count(*) AS n FROM employee_files WHERE resolved_at IS NULL AND created_at < now() - interval '2 days'"),
    Check("drafts_stale", "юборилмай қолган топшириқ қораламалари (1 кундан ортиқ)", "task_drafts",
          "SELECT count(*) AS n FROM task_drafts WHERE status = 'open' AND created_at < now() - interval '1 day'"),
)


@dataclass
class Finding:
    check: Check
    count: int
    fixed: int = 0


async def review(fix: bool = True) -> list[Finding]:
    """Run every check; settle the safe ones. Counts only."""
    findings = []
    for check in CHECKS:
        try:
            row = await fetch_one(check.count_sql)
        except Exception as exc:  # noqa: BLE001 — one broken check must not stop the rest
            log.warning("Check {} failed: {}", check.key, type(exc).__name__)
            continue
        count = int((row or {}).get("n") or 0)
        fixed = 0
        if fix and count and check.fix_sql:
            fixed = await store.run_fix(check.fix_sql)
        findings.append(Finding(check, count, fixed))
    return findings


def text(findings: list[Finding]) -> str:
    """The Admin Bot message: what was fixed, what needs a look — counts and /db tables only."""
    fixed = [f for f in findings if f.fixed]
    open_ = [f for f in findings if f.count and not f.fixed]
    lines = ["🧹 <b>Бот базаси текшируви</b>"]
    if not fixed and not open_:
        return lines[0] + "\n✅ Ҳаммаси тартибда."
    if fixed:
        lines += ["", "<b>Ўзи тузатди:</b>"] + [f"• {f.check.label}: {f.fixed}" for f in fixed]
    if open_:
        lines += ["", "<b>Кўриб чиқиш керак</b> (/db):"] + [
            f"• {f.check.label}: {f.count} — <code>{f.check.where}</code>" for f in open_]
    return "\n".join(lines)


async def send(run_id: uuid.UUID) -> str:
    from integrations.org_bot import admin

    findings = await review(fix=True)
    await admin.tell_admin(text(findings), run_id)
    await log_action(agent=AGENT, action="db_review", target_system="postgres", status="success", run_id=run_id,
                     mode="write", payload={f.check.key: {"found": f.count, "fixed": f.fixed} for f in findings})
    return "db_review_sent"
