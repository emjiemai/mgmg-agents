# Agents — what runs, what's left

Status against the owner's plan (*ЭМЖИЕМ AI Агентлар Тизими*, 21 agents).
Everything runs through the same engine: two Telegram bots (OPS Manager Bot
for everyone, Admin Bot for the admin) on the always-on `mgmg-api` service,
plus scheduled jobs at 08:00, 16:00 and 17:00. Last updated 2026-09-26.

## Working

| Plan | Agent | When it acts | Who gets it | Switch |
| ---- | ----- | ------------ | ----------- | ------ |
| H0 | Hub — API, database, both bots, SAP push receiver | always on | — | — |
| A1 | Daily reports: ask, remind, one follow-up on a vague report | 16:00 ask · 17:00 reminder (Mon–Fri) | employees; non-reporters in the 08:00 brief | `DAILY_REPORTS_ENABLED` (**must be `true`**) |
| A2 | Morning brief — five numbers + who didn't report | 08:00 daily | Director | — |
| A3 | Task tracker: deadlines, reminders, overdue notices, weekly scorecard | on each task · 08:00 · Friday 17:00 | employees, Director | `TASK_TRACKER_ENABLED` |
| B1 | Written permissions (EMJ-SOP-ADM-01) + payment gate by amount | on request ("ruxsat") | requester, approvers | `PERMISSIONS_ENABLED`, limits in `PERMISSION_APPROVAL_TIERS` (**not set yet**) |
| B2 | 30-day cash calendar | Monday 08:00 · any time: "pul kalendari" | Director, accountants | `CASH_CALENDAR_ENABLED` |
| B4 | Data quality check | Monday 08:00 · `/sifat` | admin (IT) | `DATA_QUALITY_ENABLED` |
| E1 | Monthly KPI per employee | 1st of the month 08:00 · any time in KPI answers | Director, HR | `MONTHLY_KPI_ENABLED` |
| — | Receivables alert (overdue debt by age) | 08:00 daily | Director | — |
| — | Q&A — the Director asks about reports, tasks, KPI, permissions, debt, cash plan, SAP data | on question | Director | — |
| — | Tasks to a department or to one named person, relays with confirmation | on message | employees, Director | — |
| — | Names and roles — every employee's typed name; admin re-asks a name or changes a role | on registration · `/xodimlar` · `/ism` | admin, employees | — |
| — | Database viewer (read-only) | `/db` on the API | admin | `DB_VIEWER_PASSWORD` |

**8 of the plan's 21** are built (H0, A1, A2, A3, B1, B2, B4, E1). Of these,
only H0 has been running long enough to call proven; the rest are in their
first weeks — the plan asks for each one's И to be measured before the next
is started (A1 and A3 are measured automatically every Friday).

## Stopped by the business

| Plan | Agent | Why |
| ---- | ----- | --- |
| A4 | Attendance (Verifix) | Verifix not connected; removed 2026-09-15 |
| F2 | Tender/lead search (Lead Agent) | built; paused 2026-09-16 (`LEAD_AGENT_ENABLED=false`) |

## Left — and what each is waiting for

| Plan | Agent | What unblocks it |
| ---- | ----- | ---------------- |
| C3 | Dead-stock sales | Full stock from SAP (the gateway push sends only 100 rows) + stock receipt dates |
| D1 | Stock & reorder signal | Full stock and sales from SAP (same push limit) |
| C2 | Sales forecast & targets | Full sales history per sales person from SAP |
| B3 | Reconciliation bank ↔ SAP ↔ 1C ↔ Didox | Access to the bank, 1C and Didox |
| C1 | Lead collector (Telegram, WhatsApp, Instagram) | WhatsApp Business API and Instagram access |
| C4 | Service & contract reminders | A list of machines installed at each customer |
| C5 | Customer win-back | Customer purchase history (SAP, complete) |
| F1 | Marketing & content plan | Instagram access (a plan-only version could be built without it) |
| G1 | Owner's personal assistant (email) | Outlook access |
| D2 | Documents & certificates | Nothing technical — rare work, the plan puts it later |
| E2 | AI operations manager | The plan says build it last, on top of A1, A2, A3, B1, B2; OPS Manager Bot already covers part of it |

**The cheapest unblock:** raising the SAP gateway push limit (or adding date
filters) on the gateway machine. It makes A2's stock and sales complete and
opens C3, D1 and C2.

## Keeping this current

Update this file and the tracker (`ЭМЖИЕМ_AI_Агентлар_Трекери`, column
ҲОЛАТ) whenever an agent changes state; each agent's details are in
`docs/agent-specs/`.
