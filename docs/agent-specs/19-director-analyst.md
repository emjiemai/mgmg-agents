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
| `sap_records` | `v_sap_gateway_latest`, one kind (sales, sales_lines, payments, payments_out, ap_open, supplier_balances, po_open, orders, inventory, stock_value, products, customers, warehouses, sales_people, equipment, service_calls, service_contracts) | text search, date filter on DocDate; sums worked out in code (cancelled left out); grouped by customer / item / warehouse |
| `onec_balances` | 1C `AccountingRegister_Хозрасчетный/Balance` + chart + counterparty catalog | account prefix (digits), as of a date; per account and per counterparty (ExtDimension1), so'm |
| `onec_turnovers` | 1C `…/Turnovers(StartPeriod, EndPeriod)` | debit/credit per account and counterparty: revenue 9010, cost 9110, expenses 94, money in/out 50/51. Field names are read from the answer (not yet seen on the live 1C) |
| `billz_sales` | BILLZ reports, any period ≤ 92 days | shops, sellers, top products, so'm |
| `attendance` | Verifix timesheet, ≤ 92 days, optional one person | late / absent / excused, per person |
| `supplier_debt_compare` | 1C 6010/6015 + 4310/4315 (live, GET) against SAP `supplier_balances` (pushed) — `integrations/onec/payables.py` | 2026-10-10. Supplier debt compared supplier by supplier (ИНН, then name; SAP's sign chosen by 1C): both totals, agree / differ / only-1C / only-SAP counts, biggest gaps with a likely reason. `excel=true`, or the Director's own words «солиштир», «excel», «файл», «жадвал» (`FILE_WORDS`): the same workbook as `scripts/ap_reconcile.py` is built in memory and sent after the text as `kreditorlik-1C-SAP-<date>.xlsx` (`Answer.files` → `ops_manager._send_files`); once per answer |
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
