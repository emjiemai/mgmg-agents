# Duties register and work profiles (2026-11-02, week 1 of the delegation plan)

**Asked by:** the Boss, tasks T-2026-11-0051 (collect the heads' tasks, KPI and plans; give tasks through the
Operational AI), T-0054 (easy to set up and change), T-0055 (automatic delegation by 30.11; daily, weekly, monthly
duties per person; the 08:45 check; «фойдасиз» tasks reviewed; Friday tuning) and the ЭМЖИЕМ МИЯ document.
**Code:** `integrations/org_bot/duties.py`. **Tables:** `duties`, `duty_changes`, `monthly_plans`.

## The register

A Google Sheet (`DUTIES_SHEET_ID`, shared with the service account as Editor), two tabs:
- **«Вазифалар»** — one row per duty: Ходим · Лавозим · Вазифа · Қачон (ҳар куни / ҳар ҳафта / ҳар ой / муддат /
  қоида / зарур бўлганда) · Кун / сана (weekday, day of month, or dd.mm.yyyy) · Соат (by when) · Мақсад (a number) ·
  Бирлик · Далил (what shows it was done) · Текшириш (SAP / Billz / Verifix / 1C / қўлда) · Фаол (ҳа / йўқ) · Изоҳ.
  Columns are found by their header (moved or added columns are fine; don't rename the headers).
- **«Режалар»** — the monthly plans: Йўналиш · Ойлик режа, $ · Жавобгар · Изоҳ.

Until the sheet is set, the register is the Boss's own example of 02.11 (`duties.SEED`: 13 people, 72 duties, the six
monthly plans). An empty sheet gets that example written into it once — from then on the sheet is the source.

**Easy to change (T-0054):** the Boss or a head edits a row; the bot reads the sheet every morning at 07:30 (the API's
round) and on Admin Bot `/vazifalar` at once. Every added / changed / removed row is kept in `duty_changes` (what it
was, what it became). A row's identity is person + duty + how often, so a new target is a change, not a new row.

## Work profiles

Each person's active duties become `employees.responsibilities` («Лавозими: … · Вазифалари (реестрдан): …») — the
work AI already reads it, so it answers knowing the person's job. Names are matched to the bot's people by their words
(any order, Latin or Cyrillic); unmatched names stay in the register as «ботда йўқ» (02.11: the technicians Рушан and
Жамолиддин — the new role **Техник** lets them register). Шоҳруҳ in the Boss's text is Эшонов Шахзод (the owner).

## Commands

- Employees, the Garmin manager: **/vazifalar** — their own duties.
- The Director: **/vazifalar** — who has how many duties (daily ones), who isn't in the bot, the monthly plans, the
  week's changes, the sheet's link.
- Admin Bot: **/vazifalar** — reads the sheet now: counts, the rows to fix (by row number), who isn't in the bot, how to
  connect the sheet (the service account's address). No targets or sums (IT sees no company figures).

## Week 2 — the daily tasks (2026-11-03, `integrations/org_bot/duty_tasks.py`)

**On / off:** ⏸ / ▶️ on the Director's `/vazifalar` and Admin Bot `/vazifalar` (`duty_switch`; no row = off — the
other side is told). Nothing goes out on a /dam day off, outside a person's working week, on their excused day, or
with `BOTS_FROZEN`.

| Time | What happens |
| ---- | ---- |
| 07:30 | the register is read (week 1) |
| 08:45 | each person's due duties become tasks (`duty_tasks`): daily → today; weekly with a weekday → that day, without → Monday–Saturday; monthly with a day → that day, without → the month (on the card Mondays, its first and last 5 days); a deadline → its last 7 days. «Қоида» / «зарур бўлганда» stay the profile only. Then **one card per person**: «📋 Бугунги вазифаларингиз», numbered lines with the target, three buttons each — **✅** done (a duty with a target then asks «нечта бўлди?» and keeps the number; done counts at once, a forgotten number never makes it missed), **⛔** not done (asks why, one sentence), **👎** «фойдасиз» (the Boss: useless tasks are analysed). The card redraws itself as items are answered. |
| 08:50 | IT's check in Admin Bot: was the register read at 07:30, how many people and tasks went out, who wasn't reached, yesterday's «фойдасиз» marks (titles and names — no targets or sums) |
| 17:30 | one reminder card «Бугун қолган вазифалар» — only to those with something still open today |
| 22:00 | today's open tasks, and weekly / monthly / deadline ones whose period ended, become «бажарилмади» |

The Director's `/vazifalar` shows today's progress per person (✅ / жами, 👎). A typed number or reason is taken for 3
hours after the tap; words without a number, or a question, go on to the usual flow.

## Next (weeks 3–4, to 30.11)

Week 3: checked against SAP / Billz / Verifix where the data is; plan vs
fact; the Friday review with the AI's suggestions (a head approves). Week 4: everyone connected, tuning, acceptance.
