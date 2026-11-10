# 19 — The Director's analyst (2026-10-07)

## Why

On 05.10 the Director typed «Дебитор». The bot asked "SAP B1, 1C Clobus or
Didox?", he picked Didox, then 1C, and both answers were "no data". The
Director shouldn't have to know where data lives. From the owner, 07.10:
the AI must think and get the right information from Billz, 1C, SAP and
Verifix itself, without the ability to change anything in them.

## How it works

1. The router (`prompt.CLASSIFY_SYSTEM_PROMPT`) still decides: task for a
   person, or a question. It must never answer a question with "which
   system?" — any question about the company's data is a question
   (`target_type="agent"`), its agent slug is only a hint.
2. The question goes to `integrations/org_bot/analyst.py`. The AI
   (`OPS_MANAGER_BOT_MODEL`, OpenRouter, OpenAI-style `tools`) gets the
   tools below, calls what it needs (several at once when independent), reads
   the results and answers — up to `OPS_ANALYST_MAX_ROUNDS` (6) rounds, then
   it must answer from what it found.
3. Where two systems hold the same thing it looks in both and says which
   figure is from where: customer debt = SAP open invoices **and** 1C
   account 40; supplier debt = `supplier_debt_compare` (1C 60 − 43 against
   SAP supplier balances, already compared); money = 1C 50/51/52.
3b. Since 2026-10-10 it also answers **accountants and finance people the
   admin gave systems to** (`22-data-access.md`): their own prompt and
   language, only the tools of their systems (`tools_for`), any other tool
   refused in `run_tool` — never attendance or the bot's own records.
3a. It knows the company (`knowledge.COMPANY_KNOWLEDGE`, 2026-10-10): the
   businesses, branches, departments, which system holds what and the
   Director's words. When a question can mean two clearly different things
   it asks ONE short question with the readings numbered ("1) … 2) …")
   instead of guessing — never which system, never the period.
4. If the analyst can't run (AI down, the model refuses tools,
   `OPS_ANALYST_ENABLED=false`), the old one-source answer
   (`ops_manager._answer_from_agent`) still replies.

## Tools (all read-only)

| Tool | Reads | Notes |
| ---- | ----- | ----- |
| `data_sources` | what's connected, SAP push freshness per kind | Didox: not connected |
| `sap_receivables` | `v_ar_aging_latest` (SAP OINV, open) | filters: customer, days unpaid; totals by age and customer; amounts as written (so'm) when the gateway sends them, else SAP's USD |
| `sap_records` | `v_sap_gateway_latest`, one kind (sales, sales_lines, payments, payments_out, ap_open, supplier_balances, po_open, orders, inventory (capped), stock_value, stock_items (every item in stock, 2026-10-16), products, customers, warehouses, sales_people, equipment, service_calls, service_contracts) | text search, date filter on DocDate — kinds with no dates (supplier balances, stock, customers…) ignore a period and say so (15.10.2026: a period dropped every supplier); sums worked out in code (cancelled left out; `*Sy` / `*Sys` labelled so'm); grouped by customer / item / warehouse |
| `onec_balances` | 1C `AccountingRegister_Хозрасчетный/Balance` + chart + counterparty catalog | account prefix (digits), as of a date; per account and per counterparty (ExtDimension1), so'm |
| `onec_turnovers` | 1C `…/Turnovers(StartPeriod, EndPeriod)` | debit/credit per account and counterparty. **This company's** revenue is 9020.1 (goods) and 9030.1 (services), cost of sales 9120.1 — 9010 / 9110 have no entries (checked live 15.10.2026); expenses 94 (9410, 9420, 9430), money in/out 50/51. An empty account names the accounts of the same group that do have rows |
| `billz_sales` | BILLZ reports, any period ≤ 92 days | shops, sellers, top products, so'm |
| `attendance` | Verifix timesheet, ≤ 92 days, optional one person | late / absent / excused, per person |
| `supplier_debt_compare` | 1C 6010/6015 + 4310/4315 (live, GET) against SAP `supplier_balances` (pushed) — `integrations/onec/payables.py` | 2026-10-10. Supplier debt compared supplier by supplier (ИНН, then name; SAP's sign chosen by 1C): both totals, agree / differ / only-1C / only-SAP counts, biggest gaps with a likely reason. `excel=true`, or the Director's own words «солиштир», «excel», «файл», «жадвал» (`FILE_WORDS`): the same workbook as `scripts/ap_reconcile.py` is built in memory and sent after the text as `kreditorlik-1C-SAP-<date>.xlsx` (`Answer.files` → `ops_manager._send_files`); once per answer |
| `billz_sap_check` | `billz_sap_checks`, last 7 days | 15.10.2026. Shop cheques not in SAP / wrong amount / entered late / SAP documents with no cheque, by number. Given to accounting with Billz + SAP; the Garmin manager has it as `garmin_data` → `billz_sap` |
| `company_data` | the bot's own readers (`ops_manager._fetch_agent_data`) | tasks, daily reports + KPI, permissions, cash calendar, complaints, lead hand-out, leads sheet, Garmin bot leads, Garmin catalog, brief history |

## Read-only, by construction

- The model never writes SQL, a URL or a path: only the functions above,
  with arguments validated in code (kinds and sources are fixed lists, dates
  parsed and clamped to ≤ 92 days and not in the future, account codes
  digits only, limits capped).
- Database reads use `db.fetch_read_only` — a `READ ONLY` transaction with
  a statement timeout. `analyst.py` has no `execute`.
- 1C goes through `OneCClient` (GET only); BILLZ and Verifix through their
  report reads. None of these clients has a method that changes data.
- A tool that fails returns "unavailable" to the model; one answer is cut at
  30,000 characters with a note to narrow the search.
- Tests (`scripts/selfcheck.py`, `test_analyst`): no write statement or raw
  HTTP in the module, every tool handled, arguments bounded, the loop, the
  fallback; the supplier-debt summary, the workbook only when asked, the
  text before the file.

## Privacy

Only the Director's questions come here; employees' work AI has no company
data. Logs keep only which tools ran, how long, how many characters — never
the question, the data or the answer (the Director's order of 07.10.2026:
IT doesn't see figures or management correspondence). Each answer is
recorded in `agent_actions` as `analyst_answer` (tools and rounds only), a
fallback as `analyst_fallback`; IT's technical report counts them.

## Needs on the 1C side

The OData user must have, on the «Состав» tab: `ChartOfAccounts_Хозрасчетный`,
`AccountingRegister_Хозрасчетный` (Balance, Turnovers) and, for names,
`Catalog_Контрагенты`. Payroll and personal data should stay unticked
(`/1c` warns when they're open).

## Settings

`OPS_ANALYST_ENABLED` (default true), `OPS_ANALYST_MAX_ROUNDS` (default 6),
model: `OPS_MANAGER_BOT_MODEL` / `OPS_MANAGER_BOT_FALLBACK_MODELS`.

## 2026-10-20 — old stock, live prices

- `old_stock` tool (systems: SAP): the same list as `/eski` — a year or more / within 30 days of a year, value (USD),
  warehouse, sold in 90 days; `garmin_only` for the Garmin warehouses.
- `garmin_data` `catalog`: our prices live from garmin.com.uz (`competitors.our_catalog_text`); the 21.08 snapshot
  only when the site can't be read.
- 1C answered «410 Gone» on every OData address (20.10.2026) while the web client worked: the database's OData
  publication is off on Clobus — the analyst says 1C can't be read.

## 2026-10-21 — replies, speed, grouping, short answers (the Director's SAP test)

- **Every answer replies to its question** — the Director's too (answers, questions back, refusals, the
  Excel file), as every other role's already did; several questions sent at once can be told apart.
- **Faster:** a round's lookups run together (`asyncio.gather`), and one SAP kind is read from the database
  once per answer (`_kind_rows`, kept only while that answer runs), searched in memory after that.
- **sap_records** `group_by` (any field — `SlpCode` with the sales person's name, `CardName`, `customer`,
  `WhsName`…), `sum_field`, `fields` (only those fields in the rows), `sort` (newest / oldest / amount);
  rows default 30. Display dummies are left out of stock kinds (`include_dummies` to count them).
  Amounts from 1 000 up are whole numbers.
- **old_stock** `idle_90d`: on hand, nothing sold in 90 days, any age, most valuable first.
- **Answer rules** (all roles): the answer first with the key figure in bold, at most 8 «• » lines, one
  titled block per question, whole so'm, one source line in italics; no SAP field names, "gateway",
  HTTP codes or method notes.
- Gateway (`sap-gateway/src/complete.js`, to be copied to the gateway computer): customers' `CreateDate`,
  service calls' `TechnicianName` / `AssigneeName`.

## 2026-10-22 — one person's report

`employee_report` (systems: garmin — so the Director and the Garmin manager): one person's report row(s) for a
day or up to 14 days — sent or not, «[чатдан тузилди]», the text (voice / video already as text) — and up to 10
of its files, which the bot sends after the answer. The Garmin manager finds only his team (garmin_sotuv).

**Stock = quantity (2026-10-27, Murod aka):** the Garmin brief's «Garmin қолдиғи» row — pieces on hand in the Garmin warehouses, models, the two biggest warehouses (one row, no block). For the Garmin manager «қолдиқ / остаток / кол-во / нечта қолди» = OnHand pieces from `stock_items`, per warehouse and model; money only when asked.

**Garmin stock (2026-10-27, Murod aka), `integrations/garmin/stock_list.py`:** every Garmin product left — name and pieces per Garmin warehouse and in total (SAP `stock_items`; the faulty-goods warehouse and display dummies left out). `/qoldiq` → summary + .xlsx; analyst tool `garmin_stock_list` (system sap_garmin; file=true attaches the .xlsx). Quantities only — the owner: the bot never advises what to buy (no marks, no sales pace).

## Schema first, then rows (2026-11-09)

The owner: don't send the AI whole data sets — send the structure and let it fetch only the rows it needs.

- **Views** (`database/schema.sql`, schema `analyst`): typed, question-shaped views over the latest SAP push and the
  bot's own records — `sap_sales_docs`, `sap_sales_lines`, `sap_payments_in/out`, `sap_ap_open`,
  `sap_supplier_balances`, `sap_customers`, `sap_products`, `sap_stock`, `sap_stock_by_warehouse`,
  `sap_item_sales_90d`, `sap_po_open`, `sap_orders_open`, `sap_service_calls`, `sap_equipment`,
  `sap_service_contracts`, `sap_warehouses`, `sap_sales_people`, `ar_open`, `sales_month`; Director only:
  `bot_tasks`, `bot_daily_reports` (states, never texts). Amounts keep their unit in the name (`_usd` SAP local,
  `_som` so'm, `_doc` the document's currency); dates are the date part of SAP's text (no timestamp casts);
  `analyst.num` / `analyst.day` turn a stray value into NULL instead of failing the query; `name_key` /
  `item_key` = one Latin skeleton for Cyrillic and Latin names (`analyst.name_key`, mirrored by
  `common/translit.skeleton`), `analyst.similarity` (pg_trgm) ranks close spellings.
- **Catalog** (`integrations/org_bot/sql_catalog.py`): the rules and each allowed view's columns, appended to the
  analyst's system prompt — the same text per asker (no dates, no figures), so the provider can cache it. One
  source for the prompt, the validator's allow-list and the role.
- **query_db** (`analyst.py`): one SELECT per call; `sql_guard.validate` (sqlglot) refuses anything but one read-only
  SELECT over the asker's views (no writes/DDL/INTO/FOR UPDATE/COPY/SET, no comments or dollar quotes, functions on
  an allow-list, no system columns or pg_* names); `db.fetch_analyst` runs it as `mgmg_analyst_director` (all views)
  or `mgmg_analyst_sap` (SAP views — accounting with SAP access) — NOLOGIN roles that may SELECT those views and
  nothing else — in a rollback-only READ ONLY transaction, `search_path = analyst`, 4 s statement / 1 s lock
  timeout, as a prepared statement (the server refuses a second statement), ≤ 200 rows, ≤ 8,000 characters back.
  A refusal or an SQL error goes back to the model with the reason and the view's columns, once; after two the
  model uses the other tools. Logs and the audit row keep counts and view names only — never the SQL or the rows.
- **Not set up** (no CREATEROLE on some database): the roles block in schema.sql only raises a notice; query_db is
  then simply not offered (`db.analyst_sql_ready`). The Garmin manager keeps garmin_sap / garmin_stock_list.
- **Speed, measured before the change (14 days):** a Director question p50 26.7 s / p90 116.7 s, ~95 % of it AI
  round trips; the router alone ~8 s; the answer round grows with prompt size (10–20k chars ≈ 7 s, 30–40k ≈ 17 s);
  `v_sap_gateway_latest` ran a subquery per row (GROUP BY 1.3 s, one kind 125 ms). Changes: the view joins each kind's
  max day (6 ms / 2 ms), tool output capped at 12k characters (was 30k), a question-shaped Director message starts
  the analyst while the router still decides (`ops_router_speculative`; dropped if the router says task /
  clarify / refusal), query_db replaces reading whole kinds; every AI call's audit row now records prompt, cached,
  reasoning and completion tokens (counts only).
- **Settings:** `ANALYST_SQL_ENABLED` (true), `ANALYST_SQL_TIMEOUT` (4s), `ANALYST_SQL_MAX_ROWS` (200),
  `OPS_ROUTER_SPECULATIVE` (true).
- **Tests:** `test_analyst_sql_1109` — the validator's refusals and passes, scopes and roles, catalog = schema
  columns, grants, start-up safety, the faster view, Python ↔ SQL skeleton parity, query_db's table / retry /
  counts-only state, fetch_analyst's statements in order, the early start and its cancellation.
- **Review of 09–10.11 (two reviewers; fixed):** the server applies the row limit (`SELECT * FROM (…) AS q LIMIT n+1`
  — a client cursor had loaded a runaway join into the app first); `WITH RECURSIVE` refused (memory); views dropped
  and rebuilt on every start (a changed column can't stop the start-up); sales documents/lines are **14 days** (not
  45); line money is before whole-document discounts (money totals from `sap_sales_docs` / `sales_month`),
  `signed_quantity` for returns; `sap_stock.is_faulty` (faulty warehouses aren't stock to sell); `ar_open` never adds
  as-written and local amounts (written_currency empty when there's no as-written amount); `bot_daily_reports` keeps
  people who left (`person_active`) and shows `auto_built`; `name_key` folds қ/к/q, ў/у/o', дж/ж, word-initial ye/е.
- **Open (owner's call):** (1) the role is switched with SET LOCAL ROLE on the app's login, so the guard's function
  allow-list is what keeps SQL from switching back — a separate LOGIN role for query_db (a password secret in Render)
  would make the database a hard lock; (2) Postgres writes failing statements (with any names the Director typed) to
  Render's database log, readable by IT — bound parameters for literals, or limiting who reads those logs;
  (3) `sales_facts` keeps lines of an invoice cancelled after it was collected (plan_facts.collect deletes only
  documents still pushed) — to fix in plan_facts.

**Status line (2026-11-10, `progress.py`):** one silent message under the question («⏳ Саволни тушуняпман…»), edited from the tools each round actually calls (query_db by the views its SQL reads), «⏳ Топганларимни жамлаб, жавобни ёзяпман…» after the lookups, deleted before the answer; ≤ 1 edit / 1.5 s; Telegram failures ignored. Started with the early (speculative) analyst; deleted if the router says it was a task.
