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

## Next (weeks 2–4, to 30.11)

Week 2: 08:45 — each person's tasks for the day from the register, one morning card, ✅ with the number, the end-of-day
check, «фойдали / фойдасиз»; IT's 08:45 check. Week 3: checked against SAP / Billz / Verifix where the data is; plan vs
fact; the Friday review with the AI's suggestions (a head approves). Week 4: everyone connected, tuning, acceptance.
