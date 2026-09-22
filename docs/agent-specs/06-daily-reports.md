# Daily reports + employee KPI

**Code:** `agents/daily-reports/agent.py`, `integrations/org_bot/kpi.py`, the
reply half in `integrations/org_bot/ops_manager.py`
**Schedule:** 16:00 Asia/Tashkent ask, 17:00 reminder, Mon-Fri — **only when
`DAILY_REPORTS_ENABLED=true`** (off by default; paused 2026-09-18 while not in
use). Off means nothing is sent, no rows open, and the morning brief hides its
"didn't report" section.
**Mode:** writes to `daily_reports` + Telegram; no external system is touched
**Owner:** Operations Director

## Purpose

Give the Director a daily, per-employee record of what everyone did — without
anyone having to chase people for it. The office empties at 18:00, so the ask
goes out at 16:00 while there is still time to answer, and one reminder
follows at 17:00.

## Flow

1. **16:00** — every active employee except the Director gets one message
   asking what they did today. A `daily_reports` row opens for each person
   with status `asked`, which is what makes "who never answered" answerable
   at all.
2. **The employee replies** in Telegram. OPS Manager Bot saves the text,
   counts the tasks they completed today from `tasks` (not self-reported),
   marks the row `submitted`, and thanks them. The report is **not**
   forwarded to the Director (the business's decision, 2026-09-16).
3. **17:00** — anyone still at status `asked` gets exactly one nudge
   (`reminded_at` guards against repeats).
4. **08:00 next morning** — the CEO Daily Brief names only who didn't report
   on the last day people were asked ("🔴 Hisobot yubormaganlar: 2 / 9", with
   names), or says everyone did. It uses the last *asked* day, so Monday's
   brief shows Friday. This is the only daily-report signal pushed to the
   Director.
5. **Any time** — the Director can still ask the bot "kim bugun hisobot
   yubormadi?", "bugungi reportlar", "xodimlar KPI", and the `xodimlar_kpi`
   agent answers from the last 14 days of rows, report text included.

## What is asked, and how KPI is measured

Everyone — sales included — is asked only what they did today, in their own
words. No numbers are requested (the business's decision, 2026-09-16).

KPI is measured on:
- **Reports submitted** out of days asked (a row per person per day, so a
  missed report is a recorded fact, not an absence of data)
- **Tasks completed**, counted from the `tasks` table

### Turning numbers back on

`integrations/org_bot/kpi.py` still defines the sales numbers
(`SALES_METRICS`: meetings / calls / proposals / new leads, daily targets
4 / 15 / 6 / 2 — the business's weekly CRM targets 20 / 75 / 30 / 10 over five
days), but no role is mapped to them. To ask a role for numbers again, map it
in `ROLE_METRICS` (e.g. `{"b2b_sotuv": SALES_METRICS}`): the ask and reminder
show the format, replies are parsed, the Director's card shows each number
against target, and the KPI answers include them — no other change needed.

When numbers are on, parsing is deterministic regex, not an AI call, and an
unrecognized number is never invented — it shows as `—`.

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

**Re-run a test on the same day** (each person is asked once per day):

```sql
DELETE FROM daily_reports WHERE report_date = CURRENT_DATE;
```

## Future

- Weekly Monday scorecard per employee (reports submitted out of working
  days, tasks completed) — the data is already there.
- Saturday schedule, if the working week changes (currently Mon-Fri).

## Vague reports

If a report says nothing concrete ("ok", "ishladim", "hammasi yaxshi"), the AI
asks **one** short follow-up question. The first answer is already saved, so
the employee counts as reported either way; whatever they send next is added to
the report and nothing more is asked. Reports longer than 120 characters are
never checked, and if the AI is unavailable the report simply stands.

## Late replies

- **Late report.** The employee's most recent ask stays open for up to 3 days,
  until the next 16:00 ask replaces it, so a report written after midnight (or
  a Friday report sent on Monday) is saved against the day it was asked for,
  with a "kechikib" note, instead of being relayed to the Director. A reply to
  the ask always counts. A late message that is *not* a reply counts only if
  the AI confirms it is a work report ("bugun kech kelaman" is not). If the AI
  is unavailable, it does not count.
- **Late follow-up answer.** The one follow-up question waits 3 hours. After
  that it lapses: the report stands as first sent, and later messages are
  never glued onto it.
