# Daily reports + employee KPI

**Code:** `agents/daily-reports/agent.py`, `integrations/org_bot/kpi.py`, the
reply half in `integrations/org_bot/ops_manager.py`
**Schedule:** 16:00 Asia/Tashkent ask, 17:00 reminder, Mon-Fri
**Mode:** writes to `daily_reports` + Telegram; no external system is touched
**Owner:** Operations Director

## Purpose

Give the Director a daily, per-employee record of what everyone did and how
their numbers compare to target — without anyone having to chase people for
it. The office empties at 18:00, so the ask goes out at 16:00 while there is
still time to answer, and one reminder follows at 17:00.

## Flow

1. **16:00** — every active employee except the Director gets one message
   asking what they did today. Sales roles are also shown their numbers
   format. A `daily_reports` row opens for each person with status `asked`,
   which is what makes "who never answered" answerable at all.
2. **The employee replies** in Telegram. OPS Manager Bot saves the text,
   parses any numbers, counts the tasks they completed today from `tasks`
   (not self-reported), marks the row `submitted`, and forwards a card to the
   Director immediately.
3. **17:00** — anyone still at status `asked` gets exactly one nudge
   (`reminded_at` guards against repeats).
4. **Any time** — the Director asks the bot "kim bugun hisobot yubormadi?",
   "Dmitriy KPI", "bugungi reportlar", and the `xodimlar_kpi` agent answers
   from the last 14 days of rows.

## What each role reports

`integrations/org_bot/kpi.py` is the single place this is defined. Today:

| Role | Numbers asked | Daily target |
| ---- | ------------- | ------------ |
| B2B Sotuv, Garmin Sotuv | Uchrashuvlar / Qo'ng'iroqlar / Yuborilgan KP / Yangi lidlar | 4 / 15 / 6 / 2 |
| Every other role | none — written report only | — |

The sales targets are the business's own weekly targets from the CRM's
manager report (20 / 75 / 30 / 10) divided across a five-day week. To give
another role numbers, add its tuple to `ROLE_METRICS` — the ask, the parser,
the Director's card and the KPI answers all follow with no other change.
Non-sales roles are still measured on reports submitted and tasks completed.

## Parsing replies

Deterministic regex, not an AI call (one reply per person per day, in narrow
formats, and it stays testable offline in `scripts/selfcheck.py`). Recognized:

- Labelled, in any of the three languages the team writes in:
  `Uchrashuvlar 3, qo'ng'iroqlar 22, KP 5, yangi lidlar 2`, `3 ta uchrashuv`,
  `встречи: 4, звонки: 30`
- Bare positional, only when no label matched: `3/20/5/2`

A number that isn't recognized is never invented — the metric stays unset and
shows as `—`, so nobody's scorecard gains figures they didn't report. If the
numbers are missing, the bot says which ones, and a follow-up message with
them merges into the same report (`merge_report_metrics`).

## Which message counts as the report

A reply to the 16:00 ask always counts. Otherwise, a message counts as the
report only when nothing else claims it: if it replies to a task card, or the
employee has exactly one open task, it is treated as a task update as before.
This keeps the pre-existing task conversation working on report days.

## Failure behaviour

- Telegram rejects one person's ask → logged, the row stays `asked`, so the
  17:00 reminder retries them and they still count as not reported.
- `BOTS_FROZEN=true` → nothing is sent and no rows open, so nobody is recorded
  as missing a report they were never asked for.
- The agent only requires `OPS_MANAGER_BOT_TELEGRAM_BOT_TOKEN`; unrelated
  placeholders (SAP, CRM, search) do not block it.

## Runbook

```bash
python agents/daily-reports/agent.py --ask --dry-run   # who would be asked
python agents/daily-reports/agent.py --ask             # 16:00 job
python agents/daily-reports/agent.py --remind          # 17:00 job
```

**Who hasn't reported today:**

```sql
SELECT e.display_name, r.status, r.reminded_at
FROM daily_reports r JOIN employees e ON e.id = r.employee_id
WHERE r.report_date = current_date ORDER BY r.status, e.display_name;
```

## Future

- Weekly Monday scorecard per employee (reports submitted out of working
  days, tasks completed, KPI actual vs target) — the data is already there.
- Targets per employee rather than per role, once the business sets them.
- Saturday schedule, if the working week changes (currently Mon-Fri).
