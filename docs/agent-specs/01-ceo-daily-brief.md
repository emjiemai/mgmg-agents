# Agent 1 — CEO Daily Brief

**Code:** `agents/ceo-daily-brief/agent.py`
**Schedule:** 08:00 Asia/Tashkent (03:00 UTC), daily
**Mode:** read-only from every source; the only write is one Telegram message
**Owner:** automation team

## Purpose

Replace the CEO's morning round of five logins with one Telegram message. The
brief answers five questions in a fixed order, so it can be read in fifteen
seconds and the order itself carries meaning:

1. How much cash do we have? (SAP)
2. Who owes us money and how late are they? (SAP)
3. What is in the pipeline and what is stalling? (MGMG's own CRM)
4. Who is not at work? (Verifix)
5. What tasks are overdue? (Microsoft Planner)

## Inputs

| Source | Data | Method |
| ------ | ---- | ------ |
| SAP B1 | Cash/bank G/L balances | `GET /ChartOfAccounts` |
| SAP B1 | Open A/R invoices, aged | `GET /Invoices` filtered to `bost_Open` |
| MGMG CRM | Open deals, stages, next-task status | `GET /api/external/{deals,manager-tasks,stats}` — see `integrations/crm/client.py` |
| Verifix | Attendance exceptions | Daily CSV export (API when the token exists) |
| Graph  | Overdue Planner tasks | `GET /groups/{id}/planner/plans` → `/tasks` |

Migrated off amoCRM on 2026-08-18 — MGMG built its own sales CRM
(`sales-crm-roan-six.vercel.app`), a read-only-by-design API (the issued key
has no write scope at all, unlike amoCRM's). Agent 2 (amoCRM Follow-up) has
*not* been migrated yet and still talks to amoCRM directly — see that agent's
spec for why the write-endpoint gap changes its design.

## Outputs

- One Telegram message to `TELEGRAM_CEO_CHAT_ID`
- One row in `daily_briefs` (headline figures, full JSON payload, exact message text)
- Snapshot rows in `cash_balance_snapshots`, `ar_aging_snapshots`,
  `amocrm_pipeline_snapshots`, `attendance_snapshots`, `planner_task_snapshots`
- Audit rows in `agent_actions` for every API call

## Severity markers

| Marker | Meaning | Triggered by |
| ------ | ------- | ------------ |
| 🔴 | Act today | any 90+ day receivable, any absent employee, negative cash account, >10 stalled deals, >10 overdue tasks |
| 🟡 | Watch | any overdue receivable under 90 days, any late employee, 1–10 stalled deals |
| 🟢 | Fine | nothing outstanding in that section |

## Failure behaviour

**The brief always goes out.** Sources are fetched concurrently and independently:

- One source fails → that section reads `⚠️ <system> unavailable`, the footer
  names the failed systems, and the error is stored in `daily_briefs.source_errors`.
- All four fail → a short failure notice is sent instead of a brief, so silence
  is never mistaken for good news.
- Telegram itself fails → the brief is still written to `daily_briefs` with
  status `failed`, and the exit code is 1 so cron surfaces it.

## Configuration

| Setting | Effect |
| ------- | ------ |
| `DAILY_BRIEF_HOUR_LOCAL` | Documentation only — the actual time comes from cron |
| `MS_PLANNER_GROUP_ID` | Required for the Planner section |
| `VERIFIX_MODE` | `csv` (default) or `api` |
| `DRY_RUN=true` | Print the message instead of sending |

## Runbook

```bash
python agents/ceo-daily-brief/agent.py --dry-run   # see today's brief, send nothing
python agents/ceo-daily-brief/agent.py            # send it
```

**Exit codes:** 0 sent · 1 built but not sent · 2 refused to run (unfilled `.env`)

**Brief did not arrive:**

```sql
SELECT brief_date, status, source_errors FROM daily_briefs ORDER BY id DESC LIMIT 5;
SELECT occurred_at, target_system, action, error_message
FROM agent_actions WHERE status = 'failure' ORDER BY id DESC LIMIT 20;
```

## Future

- Per-division briefs to division heads (same data, filtered by `division`)
- Day-over-day deltas on cash and overdue AR
- Bank balances from Kapital/Asia Alliance APIs instead of SAP-only
