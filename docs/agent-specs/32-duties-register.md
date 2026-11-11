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

## Week 2 — the daily tasks (2026-11-02, `integrations/org_bot/duty_tasks.py`)

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

## Week 3 — the system's check, plan vs fact, the Friday review (2026-11-02)

**Sales facts** (`plan_facts.py`, hourly in the API's round): SAP pushes 14 days of invoices; every line is kept in
`sales_facts`, so the month adds up. Dollars = the invoice's gross total (SAP's local currency is USD) split over its
lines by their net amounts (checked live 02.11: 102 of 102 invoices add back to their totals); the product group from
the item master (Garmin, Tanita, Primus, Parts, Londry); the salesperson from the invoice. People ↔ SAP salespeople by
name, spelling-tolerant (Ходжиев = Хожиев, Ёрқинов = Ёркинов, Мирпўлатов = Мирпулатов): 8 matched.

**The system's check of a duty** (only dollar targets with «Текшириш» SAP or Billz): SAP — the person's own invoices,
or the business named in the duty (Garmin / Primus) for someone who isn't a SAP salesperson, dead stock (items last
bought a year or more ago), spare parts (Parts); Billz — the shop's net sales at SAP's own dollar rate. Filled when a
number is typed, before the 17:30 card and at 22:00; the card shows «SAP (сотувчи): $820», and a typed number more
than 10 % above the fact gets «⚠️ тизимда тасдиқланмади» (the МИЯ document: every claim checked in the system). Calls,
visits, cleaning and the like have no system data — they stay as people report them.

**/reja** (the Director): the «Режалар» tab against the month so far — fact / plan, %, 🟢 ahead of the month's pace,
🔴 behind. Measured: Garmin (+Tanita), Primus (+Parts), spare parts (Parts), dead stock. Not measurable in SAP today:
service contracts (no amounts) and chemicals (no product group) — said, never guessed.

**Friday review** (`duty_review.py`, Fridays 16:30, once, while the daily tasks are on): each person's week (done /
total, «фойдасиз», not confirmed); the duties that need a look, **chosen by rules** — «фойдасиз» twice or more, a number
above the system's fact, done on under half of at least three days; for each, **the AI writes one change** (target,
frequency, time, merge, remove) and why, from the reasons people wrote — polite Uzbek Cyrillic checked, else the rule's
own words; and /reja's plans. To the Director with ✅ Қабул / ❌ Рад per suggestion (`duty_suggestions`); an accepted
one goes to IT to change in the register. IT gets the same without the plans' amounts.

## Week 4 — everyone connected, acceptance, PULSE (2026-11-02, `integrations/org_bot/rollout.py`)

**Who is connected** (the Director's and the admin's `/vazifalar`, the Friday review): the register's people in the
bot or not (they write to OPS Manager Bot and register — technicians with the «Техник» role), the bot's people with no
duty in the register (a head adds their rows; the Owner / Director gets none), live 02.11: 11 of 13 in the bot (not:
Рушан, Жамолиддин), no duties: Умар Хошимов.

**Acceptance** — the ЭМЖИЕМ МИЯ document's stage-1 condition, measured: «ходимларнинг ≥ 90 % 5 иш куни кетма-кет
чек-инга жавоб берди». A card day counts when at least 90 % of those who got the 08:45 card answered at least one line
that day; shown as «⏳ 3/5 кун · охирги кун 06.11: 11/12 (92 %)», then «✅ бажарилди».

**PULSE:** the register's tasks count in each person's daily score (the «tasks» 30 points: done that day 1, missed or
⛔ 0, a week's task not before it is due); and the Director's **«Режа ва факт»** card — each monthly plan as a bar
against the month's pace (green ahead, red behind), the unmeasurable plans said.

**The Garmin manager's `/vazifalar`:** his own duties and his team's day (✅ / жами, ⛔, 👎).

## To 30.11 (by hand)

Connect the register's Google Sheet (`DUTIES_SHEET_ID`) so heads can fix their rows; the technicians register; Умар
Хошимов's duties added; ▶️ on `/vazifalar`; then 5 working days in a row at ≥ 90 % — the Boss's acceptance.

## 2026-11-03

- **Safe testing:** Admin Bot `/vazifalar sinov` — today's card to the admin only (their tasks are real; nobody else's
  are made, so no other score or 22:00 close is touched); the Director's `/tahlil` — the week's review now, to him only.
- **An unreadable sheet** (03.11: `DUTIES_SHEET_ID` set but every read failed, retried every 5 minutes): the last good
  register stays (nothing added / removed), Google's reason is logged (`read_sheet`, error) and shown on Admin
  `/vazifalar`, the read is retried hourly, IT is told once a day.
- **Who didn't enter SAP** (`sap_entry.py`, `/kiritish`, the Director / the Garmin manager's team): shop cheques by Billz
  seller (missing / late / another amount), SAP invoices entered late by salesperson, invoices without a salesperson
  by the SAP user. Warehouse receipts / issues and spare parts used: not pushed by the gateway yet.

**2026-11-11 — cancelled invoices:** `plan_facts.collect` removes kept `sales_facts` documents dated inside the push window (13 days from SAP's last send) that the push no longer carries (cancelled in SAP); nothing is removed when the push is empty.
