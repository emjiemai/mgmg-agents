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
| Garmin's SAP, Billz and Garmin's own records through the bot | `data_access.ROLE_SCOPES` — comes with the role — **no confirmation by the Director** (the owner, 14.10: "we don't need access from the Boss when he is Garmin Manager"); changing the role closes it. The analyst offers `garmin_sap` (**the Garmin warehouses only** — invoices / credit notes with a line in them, their lines, stock, stock value; never other businesses' customers, debts or payments), `billz_sales`, `garmin_data` (Garmin bot leads, Garmin complaints with their ticket state, the catalog) and `data_sources`. Never 1C, attendance or the Director's records. |
| The Director is told | when someone gets the role (registration or a role change) and when it's taken away. |
| A morning brief | `agents/garmin-brief/agent.py`, 08:00 after the Billz → SAP check. |
| The Billz → SAP check | `BILLZ_SAP_CHECK_ROLES` defaults to `garmin_manager` (once the check leaves its trial); its morning brief shows the result meanwhile. |
| The Garmin catalog in the work AI | like Garmin сотув. |

## The Garmin brief

The same designed picture as the Director's (`brief.html`, now with `title`
and `foot`), five figures:

1. **Дўкон савдоси (Billz)** — yesterday, so'm, cheques;
2. **SAP'даги сотув** — yesterday's invoices minus credit notes with a line in
   the Garmin warehouses (`BILLZ_SAP_WAREHOUSES`), so'm (`DocTotalSy`), via
   `sap_check.docs_from_sap`;
3. **Garmin захираси** — SAP's stock value of those warehouses (USD);
4. **Очиқ шикоятлар** — open Garmin tickets, and how many came yesterday;
5. **Garmin бот лидлари** — yesterday, and how many want to buy now.

Blocks: the shops (when more than one sells), **Billz → SAP** (cheques not in
SAP / wrong amount, from `billz_sap_checks`), Garmin сотув people who didn't
send their daily report, the open complaints with their age in hours. A
source that can't be read is named on the page; the brief still goes. Once
a day (`agent_actions` `garmin_brief_sent`; `--force` sends again);
`--dry-run` writes the picture to a temp file. Nobody in the role → nothing.

Live Garmin bot leads are not re-sent: the Garmin bot hands each lead to a
manager itself; they're in the brief and in his questions.

## Tests

`scripts/selfcheck.py`, `test_garmin_manager`: the role and its picker
button, complaint routing, the scope (no 1C), the Director's notice, the
tools offered, `garmin_data`, the brief's SAP sum (Garmin warehouses only,
credit notes off) and stock, the five figures and blocks, the picture, the
08:00 order, the Billz → SAP recipients.
