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
is now one friendly lowercase line, **no emoji** (2026-10-19, the owner — it used
to end with one; `integrations/org_bot/tone.py`), addressed by first name plus ака/опа when the
admin set it (`names.call_name`):

| When | Message |
| ---- | ------- |
| 16:00 ask | Алишер ака, ишларингиз билан чарчамаяпсизми, илтимос бугунги ҳисоботингизни ёзиб юборинг, раҳмат каттакон, чарчаманг |
| 17:00 reminder | Алишер ака, ҳисоботингизни кутиб турибман, иш тугагунча ёзиб юборсангиз, раҳмат каттакон |
| report saved | раҳмат каттакон, ҳисоботингиз қабул қилинди, чарчаманг |
| vague report | the AI's one question, same voice: "нечта қўнғироққа жавоб бердингиз ва қанча сотув бўлди?" |
| answer added | раҳмат каттакон, ҳисоботингизга қўшиб қўйдим |
| too late | 29.09 кунги ҳисоботнинг вақти ўтиб кетибди, ҳисоботлар шу куннинг ўзида соат 22:00 гача олинади |
| any typed message while the report is owed (not a Reply to the ask) | бу бугунги ҳисоботингизми? — ҳа, ҳисобот · йўқ, бу савол (→ the work AI). **Never guessed** (2026-10-02). |
| a photo / video / file | бу расм нима учун? 🙂 — бугунги ҳисобот · директорга юбориш · бекор қилиш; report or Director: the file itself is forwarded to the Director with who sent it; the AI never reads it |
| /hisobot | today's report with ✏️ ўзгартириш (the next message is the new text) and 🗑 ўчириш (after «ҳа, ўчириш» it's owed again until 22:00). Only today's, only one's own. |

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

## Reports off for one person (2026-10-06)

Someone who shouldn't write a daily report at all: **Admin Bot → /xodimlar →
the person → 📝 Кунлик ҳисобот** (✅ ёқилган / ⛔ ўчирилган; tap again to
switch back on). The card shows "Кунлик ҳисобот (16:00): ўчирилган".

While off: no 16:00 ask and no 17:00 reminder, so no `daily_reports` row is
opened — they never appear as "didn't report" in the morning brief or the
Friday scorecard, and the KPI simply has no report figures for them (its
process / commitment parts use what else is measured; nothing is a zero).
Switching off also deletes today's still-unanswered ask; a report already
sent today stays. With no ask open, their typed messages go to the work AI
and files aren't offered "бугунги ҳисобот". `/hisobot` answers "сиздан кунлик
ҳисобот сўралмайди, ёзишингиз шарт эмас 🙂". Stored as `employees.reports_off`;
logged in `employee_changes` (field `reports`).

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

- **Reports close at 22:00** (Tashkent) of the day they were asked for (the Director, 2026-10-15; it was midnight).
  A report sent after that is not recorded; the employee counts as missed in
  the 08:00 brief. Replying to an old ask gets "hisobot muddati tugagan"
  (messages no longer go to the Director through the bot, 2026-10-02).
- **Follow-up answer.** The one follow-up question on a vague report waits up
  to 3 hours, and never past 22:00. After that it lapses: the report
  stands as first sent, and later messages are never glued onto it.

## 22:00 and minus points (2026-10-15)

The Director: «ҳисобот топширишни соат 22:00 гача қилиб қўй; ҳисобот
топширмаганларга минус балл ёзилсин, ҳар куни; ўзига ҳам борсин».

- The 16:00 ask and the 17:00 reminder say «соат 22:00 гача». After 22:00 a
  report isn't taken (a reply gets «вақти ўтиб кетибди»), ✏️ / 🗑 stop.
- At 22:00 (`integrations/org_bot/report_close.py`, checked every 5 minutes by
  the API — no new cron) every ask of that day still open gets
  `REPORT_MISS_PENALTY` points (default **1**) in `daily_reports.penalty_points`,
  once (`penalized_at`). Only active people who get asks: 📝 off and days off
  are never asked.
- The person gets one polite line: «Алишер ака, бугунги ҳисоботингиз соат
  22:00 гача келмади, шунинг учун KPI баллингиздан 1 балл айирилди, шу ойда
  жами −3 балл, эртага вақтида юборишингизни сўраймиз 🔴».
- The points come off that month's KPI total (`13-kpi.md`).
- Starts with the asks of **16.10.2026** (15.10's ask didn't say 22:00 yet;
  that day closes at 22:00 with 0 points and no message).

## Questions in the report window, held messages (2026-10-17)

The owner found reports and AI chat mixed: a question typed while the report
was owed got «бу бугунги ҳисоботингизми?», and a tap on «ҳа, ҳисобот» saved
the question as the report. Now a message with «?» goes straight to the AI
(`report_message_kind` → "question"); any other message is still confirmed
with one tap. Held messages nobody taps close at 22:00 (`pending_relays.outcome
= 'expired'`, `store.expire_held_messages`) instead of staying open for weeks.

## The report calendar (2026-10-17)

The Director's **/hisobotlar** (OPS Manager Bot, ☰ menu): one picture of the
month — people down the side, days across; green sent, red missed (the
minus), amber today before 22:00, grey not asked. `integrations/org_bot/report_calendar.py`,
template `reports_calendar.html` (a wider page, 1920 px). Text if the picture fails.

## Their own week — shift workers (2026-10-17)

Garmin shop staff (`kpi.OWN_SCHEDULE_ROLES` = Garmin сотув) work shifts,
weekdays off too. Their /xodimlar card has **seven day buttons** (Ду … Як,
✅ working / ☐ off) instead of the Saturday / Sunday pair; the week is stored as
`employees.work_days` (seven '1'/'0', Monday first). The 16:00 ask goes only on
✅ days — so a day off is never asked, never red, never a minus, grey on the
calendar and not in KPI. The first tap starts from their current week; a role
change or re-registration resets it. Everyone else: Monday–Friday plus the
weekend switches, as before.

The Garmin manager's **/hisobotlar** (his ☰ menu) is the same calendar for his
team only (`report_calendar.TEAM_OF`).

## Files as the report, the report from the day's messages, days off (2026-10-22, the owner)

- **Files** (`integrations/org_bot/report_media.py`): a file marked «бугунги ҳисобот» stays with the report
  (`employee_files.report_id`, its Telegram `file_id`). A **voice / audio** becomes text (speech to text) and a
  **video / round video** a 1–3 sentence description of the work and what is said — by the AI (OpenRouter,
  Gemini audio / video input), in the background; the line «[овозли хабар] …» takes its place in the report
  and the person is shown it (fix with /hisobot). A **photo / document** is kept unread: «[расм] caption».
  Over 20 MB (18 MB video) or unreadable: «[видео: матнга айлантириб бўлмади]», the file still kept.
- Report files are **no longer forwarded to the Director at once**: whoever asks for one person's report gets
  that row and its files (`analyst.employee_report` → `ops_manager._send_media`, by `file_id`, else copied from
  the chat) — the Director anyone's, the Garmin manager his team's. «Директорга юбориш» files still go at once.
- **The button forgotten** (`report_close.build_from_chat`, at 22:00 before the misses): what the person sent
  that day but never confirmed — text left on «бу бугунги ҳисоботингизми?», files without a purpose (voice /
  video read as text), their messages to the work AI — is given to the AI, which writes the report from their
  own facts only, or says there is none (questions, chatter, plans alone aren't one). Saved as sent with
  «[чатдан тузилди]», `auto_built`, no minus; the person is told with the text. Nothing there → missed as before.
- **Days off are grey** (`report_calendar.cell`): a day off in the person's own week (/xodimlar Ду…Як) is never
  a miss in the calendar or its minus count, unless they sent a report anyway. Once, at the first start after
  this change, unsent asks on days off were removed with their minus points (`report_close.clear_days_off_once`,
  marked `dayoff_misses_cleared` in agent_actions).

## Report bonus (2026-10-22, the owner)

Every morning (`agents/report-bonus/`, 08:00, before the explanations) each person in `REPORT_BONUS_ROLES` (garmin_sotuv) hears about yesterday's report, once (`daily_reports.bonus_at`): sent by them → `REPORT_BONUS_POINTS` (+1) into `bonus_points` and a polite thank-you with the month's total; missed, or «[чатдан тузилди]» → «бу сафар балл берилмади», politely, with how to get it. The 22:00 minus stays.
Since 2026-10-24 it is said in the person's one morning message (spec 30): a missed report's «no point» goes inside the
«where's yesterday's report?» question instead of a message of its own. An excused day (status `excused`) gets nothing.

## The AI decides; reports from 14:00 (2026-10-26, the owner)

After reading `ai_chat_turns`: reports typed before 16:00, after 22:00, on days without an ask or the next morning
went to the work AI and were lost; the AI said «ҳисоботингиз қабул қилинди» (Зиёда 22.10, Ширин 17.10) and saved
nothing; the «бу бугунги ҳисоботингизми?» tap was answered «ҳа» 39 times of 40.

- **The AI decides** (`ops_manager._classify_report`: report / other / unsure; under 12 characters never a report;
  AI unreachable → unsure). While a report is owed: report → saved, other → the work AI, unsure → the two buttons.
- **From 14:00** (`REPORT_OPEN_HOUR`), with no ask open (before 16:00, a day the person isn't asked): a report is
  saved — today's row is opened then (the 16:00 run then skips them). The 16:00 ask and 17:00 reminder stay.
- **Before 14:00**: «бугунги ҳисобот соат 14:00 дан 22:00 гача қабул қилинади» — refused ("employees might lie");
  yesterday's missed report written in the morning goes for approval (spec 29).
- **After 22:00**: today's closed ask takes it as a late report → the Director / manager approve (spec 29); a day
  without an ask: told it wasn't saved.
- **«Ҳа / хоп / ок»** within 30 minutes of a held draft or question = send it (`is_confirmation`, `latest_open_relay`).
- Data fixed 26.10: Зиёда 19, 20, 22.10 and Ширин 17.10 green with their own words («[26.10 тузатилди…]»),
  `report_cell_edits` rows so the Director is told.
- **A pointer is not a report** (2026-10-26): «Tepadagi xisobot» was saved as Абдукарим's 21.10 report. `is_pointer` (≤ 60 characters, «тепадаги / юқоридаги», «кечагидек», «как вчера», «выше», «above») is answered «ҳисоботни шу ерга … тўлиқ ёзинг» on every path — owed, no ask, a tapped or «ҳа» held message; the AI check is told the same. 21.10 set back to not sent (−1), his text kept, `report_cell_edits` row.
