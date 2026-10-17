# 24 — The database review (2026-10-17)

**Asked:** the owner, after opening /db: the bot's records were messy — held
messages open for weeks, test rows, questions saved as reports, removed
people still holding work. "An agent that reviews shallow information (it
doesn't work with platforms)."

**Code:** `integrations/org_bot/db_review.py` (the checks), `agents/db-review/agent.py`
(08:00, in the morning job before the technical report), Admin Bot **/baza** (now).

## What it checks — the bot's own tables only, never SAP / 1C / Billz / Verifix

| Check | Table | Fixed by itself? |
| ---- | ---- | ---- |
| «ҳисобот / савол» messages nobody tapped, before today | `pending_relays` | ✅ marked `expired` |
| a removed person's open tasks | `tasks` | ✅ cancelled |
| open tasks from a person's old role | `tasks` | ✅ cancelled |
| the same open task twice for one person | `tasks` | reported |
| questions («?») saved as reports | `daily_reports` | reported |
| reports under 5 characters | `daily_reports` | reported |
| test complaints / test tasks («test», «тест»…) | `client_feedback`, `tasks` | reported |
| active people without a name for a day | `employees` | reported |
| data access in a role that can't have it | `employees` | ✅ cleared |
| files nobody said what they're for (2 days) | `employee_files` | reported |
| task drafts never sent (1 day) | `task_drafts` | reported |

Fixes are UPDATEs only — `store.run_fix` refuses anything else; nothing is
deleted by the bot. What needs deleting is named with its /db table for the
owner to remove. IT gets counts only (the Director's order of 07.10.2026).
`--dry-run` checks and prints without changing or sending anything.

## Tests

`scripts/selfcheck.py`, `test_cleanup_round_17_10`.
