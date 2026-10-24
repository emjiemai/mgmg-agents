# 30 — One morning message per employee (2026-10-24, the owner)

**Asked:** "In the morning OPS Manager bot might ask multiple questions at once — Verifix, report and other
questions. Train it so it doesn't overwhelm or make mistakes: it needs to know what its job is for one specific
employee."

**Before:** at 08:00 a Garmin сотув person who missed yesterday's report got the bonus agent's «бу сафар балл
берилмади» AND the explanations agent's «ҳисоботингиз келмади, сабабини ёзинг…» about the same thing, plus one
message per task reminder («…Ҳолатни шу ерга ёзинг» — another question).

**Code:** `integrations/org_bot/morning_notes.py`, `agents/morning-notes/agent.py`, table `morning_notes`;
`integrations/org_bot/agenda.py`; `ops_manager._classify_answer`.

## The notes

The 08:00 agents that speak to employees leave a note instead of sending:

| kind | from | detail |
| ---- | ---- | ------ |
| `bonus` | agents/report-bonus | yesterday's date, the points given (0/1), the month's total, sent / missed / built from messages |
| `ask` | agents/explanations | «absent» (the days) or «report» (yesterday), the explanation ids |
| `task` | agents/task-tracker --morning | code, summary, due date, overdue or not |

`run_morning_agents.py`: brief → report-bonus → explanations → task-tracker --morning → **morning-notes** → the rest.

## The message

`morning_notes.compose(name, notes)` — pure, tested offline:
- **one thing** → one polite line with the name, as before («Ширин опа, кечаги, 23.10 ҳисоботингиз учун раҳмат…»);
- **several** → «Ширин опа, хайрли тонг» + the facts as «• …» lines (the point first, then tasks with their codes
  and words as written, and «бажарилган топшириқда «Бажардим» тугмасини босинг») + **at most one question, last**;
- a missed report's «no point» is said **inside** the report question («…келмади, шунинг учун бу сафар балл
  берилмади, сабабини…»), never as its own line;
- the report question carries **📝 Кечаги ҳисоботни юбориш** (spec 29); no emoji to employees, «сиз» everywhere.

`send_day` claims the day's unsent notes at once (no double sending), sends each active person one message, and
sets that message as the explanation's prompt (a reply answers it). A question withdrawn in the meantime (a day
Verifix didn't record, spec 27) is left out. If the 08:00 job dies, the API's 5-minute loop sends notes still
unsent after 30 minutes (08:00–12:59). Counts-only logs.

## Knowing the job for one person

- **The answer:** a reply or plain message to the question is read as reason / the missing report itself / other
  (spec 27) — so a typed report goes for approval, a word about a task goes on to the work AI.
- **The work AI** gets `agenda.pending(employee)`: the open question (and the 📝 way), a late report being sent,
  today's report (owed until 22:00, or sent). Asked «what do I have to do?», it says exactly that, asks for
  nothing else and never says these are done. `knowledge.EMPLOYEE_BOT_GUIDE` describes the morning message.

## Tests

`scripts/selfcheck.py`, `test_morning_verifix_and_admin_sheet`: compose (merged «no point», order, one question
last, one line alone, no emoji, polite), the producers write notes and send nothing, the 08:00 order, one message
per person and the prompt set, withdrawn questions left out, the agenda in the work AI's prompt, the three-way
answer check (in `test_explanations_and_start_times`).
