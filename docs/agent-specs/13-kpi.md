# KPI — the Director's criteria (E1, extended)

**Code:** `integrations/org_bot/kpi_score.py` (the rules, pure),
`integrations/org_bot/kpi_flow.py` (commands, buttons, the month),
`agents/task-tracker/agent.py --monthly` (1st and 5th of the month)
**Stored in:** `kpi_goals`, `kpi_ratings`, `kpi_periods` (+ tasks, daily reports, Verifix)
**Switch:** `MONTHLY_KPI_ENABLED` (the monthly sends; commands always work)

## The task

From the Director, 2026-09-30: "Ходимларни KPI баҳолаш учун қуйидаги
мезонларни тизимга жорий қилинг: KPI, Performance, communication,
commitment, interaction, OKR, RACI, Task, goals, results, sales deals,
qualifications, quantity of work, jobs done ва process done."

## How the 15 criteria are measured

Several are one thing under two names, and two are frameworks rather than
numbers, so they are six parts, each 0–100, each with a source that can be
checked:

| Part (weight) | Criteria | Source |
| ------------- | -------- | ------ |
| 🎯 Натижа (30%) | OKR, goals, results, sales deals | monthly goals with a number, set by the Director (`/maqsad`); actual ÷ target, 100% at most |
| ✅ Топшириқ (20%) | Task, RACI | tasks done by their deadline ÷ tasks due — scored for the assignee (**R**esponsible); the Director who gave it is **A**ccountable |
| ⭐ Раҳбар баҳоси (20%) | Performance, communication, interaction, qualifications | the Director's 1–5 marks once a month; the average × 20 (4 → 80) |
| 📦 Иш ҳажми (10%) | quantity of work, jobs done | tasks finished + daily reports sent + leads closed (dismissed or done, `14-lead-handout.md`), against the team's median (at or above the median = 100) |
| 🔄 Жараён (10%) | process done | (daily reports sent before 18:00 + 15:00 lead questions answered that day) ÷ (reports asked + lead questions asked) |
| 💪 Садоқат (10%) | commitment | attendance from Verifix ((working − absent − ½ late) ÷ working) and reports sent ÷ asked, averaged |

**KPI** is the weighted total: 🟢 80+, 🟡 60–79, 🔴 below 60.

A part with no data for someone (no goal set, no task due, never asked for
a report, Verifix not connected or the name doesn't match) is **left out and
the rest re-weighted** — never counted as zero. Someone with nothing
measured at all gets no score (⚪), not 🔴.

Sales deals are goals, not an automatic SAP number: the SAP pushes are capped
at 100 rows and orders don't reliably carry the sales person, so a count
from SAP would be wrong. The Director sets them like any goal ("15 та янги
шартнома", "сотув 500 млн сўм").

## Commands (OPS Manager Bot)

| Who | Command | What |
| --- | ------- | ---- |
| Director | `/maqsad` | pick a person → type the goal with a number ("20 та янги шартнома"). The AI checks it has a measurable number and writes it in Cyrillic; the person is told. From the 25th, goals are for next month. |
| Director | `/maqsadlar` | this period's goals with results; ✏️ enter a result, 🗑 cancel |
| Director | `/kpi` | everyone's KPI, best first |
| Director | `/baho` | the 1–5 rating cards again |
| Employee | `/kpi` | their own card: each part, the goals' progress |
| Employee | `/natija` | enter a goal's result (a number only) |

`/kpi` shows last month during the first five days (it's being closed),
then the current month so far. `/bekor` cancels a goal being typed or a
result being entered. Typed goals and results are caught before daily
reports and task routing, so "17" isn't filed as a report.

## The month

- **1st, 08:00** — the Director gets last month's table so far, then one
  rating card per person: four rows (📊 самарадорлик, 💬 мулоқот, 🤝 жамоада
  ишлаш, 🎓 малака) of 1–5 buttons. Everyone with a goal whose result isn't
  in yet gets a "✏️ Натижани киритиш" button.
- **Final table** — to the Director and HR as soon as the last card is
  rated; if some aren't, on the **5th** with whatever is in. Sent once
  (`kpi_periods`).
- The Friday scorecard (A3/A1) is unchanged.

The Director can also ask in plain words ("kim yaxshi ishlayapti?", "KPI
qanday?"): the `xodimlar_kpi` answers carry every score and part.

## Example

    📈 KPI — сентябр 2026
    🎯 Натижа (OKR) · ✅ Топшириқ · ⭐ Раҳбар баҳоси · 📦 Иш ҳажми · 🔄 Жараён · 💪 Садоқат

    1. 🟢 Алишер Каримов (B2B сотув) — 88
          🎯 75 · ✅ 100 · ⭐ 80 · 📦 100 · 🔄 100 · 💪 100
    2. 🔴 Дилноза Раҳимова (HR (кадрлар)) — 50
          📦 50 · 🔄 50 · 💪 50

## Changing it

Weights and grade lines are the `PARTS`, `GREEN`, `YELLOW` constants in
`kpi_score.py`; the four rated criteria are `RATINGS`. Change them there
and the table, the cards, the bot's answers and the tests follow.
