# 23 — The Garmin manager (2026-10-14)

**Asked:** the owner — a new role that has all of Garmin's data: every Garmin
complaint goes to him (not to Garmin сотув), he can look up SAP and Billz,
and he gets a morning brief like the Director's, with the Garmin section only.

**Role:** `garmin_manager` («Garmin менежер», `roles.py`). People pick it when
they register; the admin approves it in Admin Bot like any other role. The
database's role checks list it (`schema.sql`, both `employees_role_check`s).

## What the role gives

| What | How |
| ---- | ---- |
| Every Garmin complaint | `routes.py`: `complaint:garmin` → `garmin_manager` (tickets, ✅ + reason, hourly "ҳал бўлдими?" — `10-client-feedback.md`). Nobody in the role → the Director. |
| Garmin's SAP, Billz and Garmin's own records through the bot | `data_access.ROLE_SCOPES` — comes with the role — **no confirmation by the Director** (the owner, 14.10: "we don't need access from the Boss when he is Garmin Manager"); changing the role closes it. The analyst offers `garmin_sap` (**the Garmin warehouses only** — invoices / credit notes with a line in them, their lines, stock, stock value; never other businesses' customers, debts or payments), `billz_sales`, `garmin_data` (Garmin bot leads, Garmin complaints with their ticket state, the catalog, and — 2026-10-16 — **his team's daily reports**: the Garmin сотув people's last 14 days, what each wrote, when, who didn't send one and the minus) and `data_sources`. Never 1C, attendance, anyone else's reports or the Director's records. |
| The Director is told | when someone gets the role (registration or a role change) and when it's taken away. |
| A morning brief | `agents/garmin-brief/agent.py`, 08:00 after the Billz → SAP check. |
| The Billz → SAP check | `BILLZ_SAP_CHECK_ROLES` defaults to `garmin_manager` (once the check leaves its trial); its morning brief shows the result meanwhile. |
| The Garmin catalog in the work AI | like Garmin сотув. |

## The Garmin brief

The same designed picture as the Director's (`brief.html`, now with `title`
and `foot`), four figures:

1. **Дўкон савдоси (Billz)** — yesterday, so'm, cheques;
2. **SAP'даги сотув** — yesterday's invoices minus credit notes with a line in
   a Garmin warehouse — the shops' (`BILLZ_SAP_WAREHOUSES`) **and every SAP
   warehouse named «Garmin»** (`old_stock.garmin_codes`, 19.10.2026: 09
   zahiralar, 10 Ambasador, 03 Bodomzor, 04 Abay, 06 faulty had been left out),
   so'm (`DocTotalSy`), via `sap_check.docs_from_sap`. Labelled «08:00 гача SAP'га
   киритилгани»: the shops enter a day's sales late (19.10: 11 cheques entered
   12:05–14:39), so the 08:00 figure is what SAP had then;
3. **Очиқ шикоятлар** — open Garmin tickets, and how many came yesterday;
4. **Garmin бот лидлари** — yesterday, and how many want to buy now.

**Garmin захираси was removed** (19.10.2026, the owner) — old stock is `/eski`.
The Billz → SAP block is titled «DD.MM.YYYY · 08:00 ҳолатига»: it is the check
at 08:00; the bot's «қайси чеклар SAP'да йўқ?» checks again live.

Blocks: the shops (when more than one sells), **Billz → SAP** (cheques not in
SAP / wrong amount, from `billz_sap_checks`), Garmin сотув people who didn't
send their daily report, the open complaints with their age in hours. A
source that can't be read is named on the page; the brief still goes. Once
a day (`agent_actions` `garmin_brief_sent`; `--force` sends again);
`--dry-run` writes the picture to a temp file. Nobody in the role → nothing.

Live Garmin bot leads are not re-sent: the Garmin bot hands each lead to a
manager itself; they're in the brief and in his questions.

## Chat memory (2026-10-16)

He found the bot forgetful. Two causes, both fixed for everyone with the
analyst or the work AI: a reply to one of the bot's own messages (an earlier
answer, a complaint card, the brief) now shows the AI what was quoted
(`ops_manager.with_quote`, up to 1 500 characters) — "that one" was a guess
before; and the AI keeps the last 24 messages of the day in mind, not 12
(`AI_MEMORY_TURNS`). Task cards keep their own flow.

## His team's report calendar (2026-10-17)

**/hisobotlar** (his ☰ menu): the month's report calendar picture for the Garmin
сотув team only — green sent, red missed, amber today, grey not asked (their own
week's days off included).

## No daily report of his own (2026-10-17)

The owner: «Garmin Manager will not send hisobot, remove it completely». Like the
Director (`kpi.NO_REPORT_ROLES`): never asked at 16:00, never reminded, never a minus,
not a row on the report calendar, his messages never become a report; his card says
«сўралмайди» without the 📝 switch; his ☰ menu has /hisobotlar (his team), not /hisobot.

## Tests

`scripts/selfcheck.py`, `test_garmin_manager`: the role and its picker
button, complaint routing, the scope (no 1C), the Director's notice, the
tools offered, `garmin_data`, the brief's SAP sum (Garmin warehouses only,
credit notes off, every warehouse named «Garmin»), no stock, the four figures, the 08:00 labels, the blocks, the picture, the
08:00 order, the Billz → SAP recipients.

**Stock = quantity (2026-10-27, Murod aka):** the Garmin brief's «Garmin қолдиғи» row — pieces on hand in the Garmin warehouses, models, the two biggest warehouses (one row, no block). For the Garmin manager «қолдиқ / остаток / кол-во / нечта қолди» = OnHand pieces from `stock_items`, per warehouse and model; money only when asked.

**Stock for ordering (2026-10-27, Murod aka), `integrations/garmin/stock_list.py`:** every Garmin item on hand or sold in 90 days (SAP `stock_items`, `item_sales_90d`, `products`), per Garmin warehouse — the faulty-goods warehouse left out — total, sold 90 d, per month, months it lasts, on order, Min/Max, mark тугаган / кам (< 1 month or < Min) / етарли (1–4) / кўп (> 4) / сотилмаяпти. `/qoldiq` → summary + .xlsx; analyst tool `garmin_stock_list` (system sap_garmin; file=true attaches the .xlsx). Quantity only, no money.
