# 22 — Company data through the bot for accounting and finance (2026-10-10)

**Asked:** the Director — the accountant (Бухгалтерия) and finance (Молия)
should look up 1C, SAP and Billz through the Telegram bot; the admin gives
it to certain people. The accountant reads Russian, so the bot answers her
in Russian.

**Code:** `integrations/org_bot/data_access.py` (who, which systems, the
messages), `analyst.py` (`tools_for`, `employee_prompt`, `run_tool(allowed=…)`),
`ops_manager._answer_data_question` / `_handle_data_access_close`,
`admin.employee_card` (buttons `dacc:` and `lng:`), `store.toggle_data_access`,
`clear_data_access`, `toggle_lang`. Database: `employees.data_access`
(`'sap'`, `'1c'`, `'billz'`), `employees.lang` (`'uz'` / `'ru'`).

## Giving and closing it

1. Admin Bot → `/xodimlar` → the person → three toggles **SAP · 1C · Billz**
   (✅ given, ▫️ not). They appear only for **Бухгалтерия** and **Молия**; the
   database refuses anyone else too (`role = ANY(...)` in the update).
2. Every change: logged in `employee_changes` (old → new, by whom), the
   person is told in their language, and **the Director gets a notice in
   OPS Manager Bot** — who, which systems, given by which admin — with
   **⛔ Ёпиш**, which closes all of that person's access at once (only an
   active Director can press it; the admin and the person are told).
3. Changing the person's role to another department, or removing them,
   closes it too (the Director is told).
4. `🌐 AI тили: ўзбекча / русча` on the same card — the language the AI
   answers that person in. Set by the admin, never guessed.

## What they can ask

Their free messages (not a report, not a reply to a task card — those keep
their own flows) go to the analyst with **only the tools of the granted
systems**:

| Given | Tools |
| ---- | ---- |
| SAP | `sap_receivables`, `sap_records` (every pushed SAP kind) |
| 1C | `onec_balances`, `onec_turnovers` |
| Billz | `billz_sales` |
| SAP + 1C | also `supplier_debt_compare` (with the `.xlsx` in Uzbek and Russian) |
| always | `data_sources` (which systems are connected, how fresh) |
| **never** | `attendance` (Verifix), `company_data` (tasks, KPI, reports, permissions, complaints, cash calendar) — the Director's only |

The tools are filtered twice: only the granted ones are offered to the
model, and `run_tool` refuses any other name even if the model sends it.
The prompt tells the AI who is asking and what they may see; anything else
gets "this isn't in your access — the admin can open more with the
Director's approval". A non-data work question is answered briefly without
tools. Same daily limit as the work AI (60). A reply to a task card still
goes to the work AI.

## Language

`employees.lang = 'ru'`: the analyst and the work AI answer in Russian; the
supplier-debt workbook comes in both languages, Russian first. This is the
one exception to "every bot text is Uzbek Cyrillic" — the bot's fixed
messages (report asks, task cards, buttons) stay Uzbek.

## Privacy

IT gives the access but sees no answers (the Director's order of
07.10.2026): logs keep `data_question` with the systems, tool names and
rounds — never the question or a figure. IT's technical report shows only
how many people have access. The chat memory (`ai_chat_turns`, one day) is
the same as the work AI's and is not in `/db`.

## Tests

`scripts/selfcheck.py`, `test_data_access`: who can be given access, the
card and its buttons, the Director's notice and ⛔, tools per grant (never
attendance or the bot's records), a sneaked tool call refused in code, the
employee's prompt and Russian, routing (analyst vs work AI), the admin
flow, the database guard, the technical report's count.
