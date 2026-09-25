# B2 cash calendar · B4 data quality · E1 monthly KPI

Three agents from the owner's plan (*ЭМЖИЕМ AI Агентлар Тизими*), all built
only on data this system already has. Each runs from the 08:00 morning job
and sends only on its own day, so no new Render service was needed.

| Agent | Code | When | To whom | Switch |
| ----- | ---- | ---- | ------- | ------ |
| B2 cash calendar | `agents/cash-calendar/agent.py` | Monday 08:00 | Director + accountants (`buxgalteriya`) | `CASH_CALENDAR_ENABLED` |
| B4 data quality | `agents/data-quality/agent.py` | Monday 08:00, and `/sifat` in Admin Bot | the admin chat (IT) — never the Director | `DATA_QUALITY_ENABLED` |
| E1 monthly KPI | `agents/task-tracker/agent.py --monthly` | 08:00 on the 1st | Director + HR (`hr`) | `MONTHLY_KPI_ENABLED` |

All three default to on; set the switch to `false` in Render's `mgmg-shared`
group to pause one. Each accepts `--force` (run on any day) and `--dry-run`.

## B2 — 30-day cash calendar

    📅 30 кунлик пул календари — 28.09–27.10.2026

    ⬇️ Келиши кутилаётган (очиқ ҳисоб-фактуралар, тўлов муддати бўйича)
       28.09–04.10: $3,200.00 (1 та)
       05.10–11.10: —
       12.10–18.10: —
       19.10–27.10: $5,500.00 (1 та)
       ⚠️ Муддати ўтган, ҳали тушмаган: $7,384.36 (16 та)

    ⬆️ Кетадиган (тасдиқланган ёзма рухсатлар)
       30.09 — 15 000 000 сўм — Принтер сотиб олиш (EMJ-2026-0007)
       Жами: 15 000 000 сўм
       ❔ Санаси аниқ эмас: 1 та

    Касса қолдиғи уланмаган — бу фақат кирим ва чиқим режаси.
    Иш ҳақи ва ёзма рухсатсиз тўловлар бу ерда йўқ.

- **In:** open SAP invoices by their due date; already overdue ones are kept
  apart (expected, but late). A capped invoice feed makes this "камида".
- **Out:** approved written payments (B1) by "Бажариш муддати", read with the
  strict date reader (`integrations/common/dates.py`). An unreadable date is
  counted, never placed on a day.
- **Not claimed:** a balance (cash isn't connected), salaries, and anything
  paid without a written approval — the payment gate is what makes "out"
  complete.
- The Director can ask OPS Manager Bot any day ("pul kalendari", "keyingi
  haftada qancha pul tushadi") — the `pul_kalendari` agent answers from the
  same data.

## B4 — data quality (for IT)

What it checks, from what this system can see:

- **SAP invoices:** no sales person (they show as "Бошқа" everywhere), no due
  date, due before issued, a balance that can't be right.
- **SAP feeds:** when each last arrived (flagged after a day), whether it hit
  its push row limit (the numbers built on it are then incomplete), and rows
  whose columns couldn't be read.
- **SAP stock (what was pushed):** negative quantity, stock with no cost.
- **The bot's own records:** employees without a name (`/ismlar` fixes it),
  open tasks with no deadline for 3+ days, approved payments whose date can't
  be read (they can't go on the calendar), permission requests abandoned 3+
  days.

It reports; it never fixes anything or guesses a correction. With nothing
wrong it says "✅ тоза".

## E1 — monthly KPI

    📈 Ойлик KPI — сентябр 2026
    Ҳисобот: юборган/сўралган (ўз вақтида) · Топшириқ: ўз вақтида/муддати келган

    🔴 Бобур Алиев — ҳисобот — · топшириқ 0/1
    🟡 Дилноза Юсупова — ҳисобот 5/10 (5) · топшириқ —
    🟢 Алишер Каримов — ҳисобот 10/10 (10) · топшириқ 1/1

Two KPIs per person, measured by the bot rather than self-reported:

- **Report discipline** — reports sent of those asked, and how many before
  18:00. The plan's И: 🟢 ≥ 0.80, 🔴 < 0.50.
- **Tasks on time** — tasks done by their deadline of those that fell due.
  🟢 ≥ 0.70, 🔴 < 0.50.

A person's mark is the worse of the two; worst first. The rules are exactly
the Friday scorecard's (`task_tracker.score_reports` / `score_tasks`), per
person, so the two never disagree. The same 30-day KPI is in the
`xodimlar_kpi` answers ("kim yaxshi ishlayapti?").

The plan's third kind of KPI — sales targets per person — needs complete
sales data per sales person, which the SAP feeds don't provide yet.
