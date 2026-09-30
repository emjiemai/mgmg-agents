# Daily reports + employee KPI

**Code:** `agents/daily-reports/agent.py`, `integrations/org_bot/kpi.py`, the
reply half in `integrations/org_bot/ops_manager.py`
**Schedule:** 16:00 Asia/Tashkent ask, 17:00 reminder, every day: Monday–Friday
everyone, Saturday/Sunday only the weekend workers (see below) — **only when
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
2. **The employee writes their report** — just a message, no need to use
   Telegram's reply. OPS Manager Bot saves the text,
   counts the tasks they completed today from `tasks` (not self-reported),
   marks the row `submitted`, and thanks them. The report is **not**
   forwarded to the Director (the business's decision, 2026-09-16).
3. **17:00** — anyone still at status `asked` gets exactly one nudge
   (`reminded_at` guards against repeats).

**How it sounds (2026-09-30).** Employees felt the old ask — "🕓 **Кунлик
ҳисобот**" and two paragraphs — as one more alarm. Every message in this flow
is now one friendly lowercase line with one emoji at the very end
(`integrations/org_bot/tone.py`), addressed by first name plus ака/опа when the
admin set it (`names.call_name`):

| When | Message |
| ---- | ------- |
| 16:00 ask | алишер ака, ишларингиз билан чарчамаяпсизми, илтимос бугунги ҳисоботингизни ёзиб юборинг, раҳмат каттакон, чарчаманг 🙏 |
| 17:00 reminder | алишер ака, ҳисоботингизни кутиб турибман, иш тугагунча ёзиб юборсангиз, раҳмат каттакон 🙏 |
| report saved | раҳмат каттакон, ҳисоботингиз қабул қилинди, чарчаманг 😊 |
| vague report | the AI's one question, same voice: "нечта қўнғироққа жавоб бердингиз ва қанча сотув бўлди? 🙂" |
| answer added | раҳмат каттакон, ҳисоботингизга қўшиб қўйдим 😊 |
| too late | 29.09 кунги ҳисоботнинг вақти ўтиб кетибди, ҳисоботлар шу куннинг ўзида соат 24:00 гача олинади 🙂 |
| report or message? | бу бугунги ҳисоботингизми? 🙂 — buttons: ҳа, ҳисобот · директорга хабар · бекор қилиш |

`selfcheck.py` fails if any of these gains a capital letter, a line break,
markup, or an emoji anywhere but the end. Task cards keep their format.
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

Nobody has to use Telegram's reply feature (2026-09-25 — employees found
"reply to the bot's message" confusing):

| The message is… | It is… |
| --------------- | ------ |
| a reply to the 16:00 ask or the 17:00 reminder | the report |
| a reply to a task card | a task update (goes to the Director after "юборилсинми?") |
| a plain message, no task in flight | the report |
| a plain message while a task is in flight | unclear — the bot asks once: **📝 Ҳа, ҳисобот · 📨 Директорга хабар · ❌ Бекор қилиш** |

The choice replaces the usual "send to the Director?" confirmation, so it's
never an extra tap. The logic is `ops_manager.report_message_kind`, tested in
`scripts/selfcheck.py`.

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

## Weekend workers

Everyone is asked Monday–Friday. Some people also work on Saturday or Sunday:
the admin opens **Admin Bot → /xodimlar → the person** and taps **☐ Шанба** /
**☐ Якшанба** (✅ = on; tap again to switch off). The card then shows
"Иш кунлари: Душанба–Жума + шанба" and the list marks them "(+ шанба)".

On Saturday and Sunday the crons still run, but only those people are asked
and reminded, so nobody else shows up as "didn't report" in the morning
brief. Stored as `employees.works_saturday` / `works_sunday`; every switch is
logged in `employee_changes` (field `workdays`). The Friday scorecard covers
Monday–Friday; weekend reports count in the monthly KPI.

## Vague reports — one question for a little accuracy

The AI asks **one** short follow-up question when a report says nothing
concrete ("ok", "ishladim"), or lists general activities with nothing
checkable for the ones that matter — e.g. "отвечала на звонки, консультировала
клиентов, продажи" gets "Нечта қўнғироққа жавоб бердингиз, нечта мижозга
маслаҳат бердингиз ва бугун қанча сотув бўлди?". It is not asked when the main
activities already carry a number, a client or company name, a document or a
result, and never about chores (cleaning, tidying displays). It sees the
employee's role, so the question fits the job. When in doubt, it doesn't ask.

The first answer is already saved, so the employee counts as reported either
way; whatever they send next is added to the report and nothing more is asked
("a little accuracy is enough", 2026-09-28). Reports over 3000 characters and
AI outages skip the check.

## Late replies

- **Reports close at midnight** (Tashkent) of the day they were asked for.
  A report sent after that is not recorded; the employee counts as missed in
  the 08:00 brief. Replying to an old ask gets "hisobot muddati tugagan"
  instead of being relayed to the Director.
- **Follow-up answer.** The one follow-up question on a vague report waits up
  to 3 hours, and never past midnight. After that it lapses: the report
  stands as first sent, and later messages are never glued onto it.
