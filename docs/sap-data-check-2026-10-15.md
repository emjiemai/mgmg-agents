# SAP data and the Director's brief — checked one by one (15.10.2026)

The Director's 08:01 brief of 15.10.2026 was called "completely false". This
page checks every SAP data kind the system receives, then every line of the
brief, against the data itself. Counts, dates and column names only — no
amounts (the Director's order of 07.10.2026). Each row says how an accountant
verifies it in SAP / 1C.

Legend: ✅ right · ⚠ right arithmetic, misleading · ❌ wrong · 🔧 fixed in code
(15.10) · ✋ needs a person.

## 1. What SAP sends us (every 30 minutes, from the gateway computer)

| Our name | SAP table | Gateway tool | Rows now | Complete? | Used by | Status |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| `ar_open` → `ar_aging_snapshots` | OINV, open (DocStatus O, not cancelled) | `get_open_invoices` | 44 invoices | ✅ complete | brief «Мижоз қарзи», receivables alert, cash calendar, `sap_receivables` | ⚠ **17 of the 44 rows re-written every :17 / :47** by the old `get_invoices` (100 rows) from an **old duplicate Windows task**, and the list marked "capped" — so the brief said «камида». *Corrected later on 15.10:* the total stayed the same as the complete list's (the accountant's question at 17:30 gave the same 256,86 млн from the complete list). 🔧 the old push is now ignored when the complete list is newer (seen working at 12:17 and 12:47 UTC); ✋ delete the old task. |
| `sales` | OINV + ORIN headers, dated or entered in the last 14 days | `get_sales_by_date` | 72 documents (all invoices, no credit notes; all DocCur = UZS) | ✅ | brief «Кечаги сотув», Billz → SAP, Garmin brief | ⚠ the brief sums `DocTotal` = SAP's local currency **USD**, though the documents are in so'm (`DocTotalSy`); and only invoices **dated** yesterday **and already entered** at 08:00 — the shop enters a day late. |
| `sales_lines` | INV1 + RIN1 of those | `get_sales_by_date` | 112 lines; warehouses G.A._01, 15, 07, 14, 08, 02, 05, G.A._02 | ✅ | Billz → SAP, `garmin_sap` | ✅ |
| `stock_value` | OITW (+ OITM) summed per warehouse | `get_stock_value` | 18 warehouses | ✅ | brief «Захира», Garmin brief, analyst | ❌ **StockValue = 0 in all 18**: the gateway multiplied OnHand by the item-level price OITM.AvgPrice, which is 0 here. 🔧 `sap-gateway/src/stock-value.js` now uses SAP's own per-warehouse value (OITW.StockValue, then OITW.AvgPrice). ✋ copy `sap-gateway/src/` to the gateway computer and restart. |
| `supplier_balances` | OCRD, suppliers | `get_supplier_balances` | 62 | ✅ | 1C ↔ SAP supplier debt | ✅ (checked 09–10.10) |
| `inventory` | OITW + OITM | old `get_inventory` | 100 (5 per warehouse) | ❌ capped | analyst «қолдиқ», data quality | ⚠ useless for an item («Fenix 8 нечта?») — the item rows of `get_stock_value` should be kept for that (next step). |
| `orders` | ORDR | old `get_orders` | 103 | ❌ capped | analyst | ⚠ partial |
| `payments` | ORCT | old `get_payments` | 103 | ❌ capped | analyst | ⚠ partial |
| `customers` | OCRD, customers | old `get_customers` | 100 | ❌ capped | analyst | ⚠ partial |
| `products` | OITM | old `get_products` | 20 | ❌ capped | analyst | ⚠ partial |
| `warehouses` | OWHS | `get_warehouses` | 20 | ✅ | names | ✅ |
| not arriving | OVPM (payments to suppliers), OPCH (supplier invoices), OPOR (purchase orders), OINS / OSCL / OCTR (service) | — | 0 | — | — | ✋ new gateway tools (`docs/sap-gateway-tools.md`) |

## 2. The brief of 15.10.2026 08:01, line by line

| Line | Where from | Verdict | Why | How to check |
| ---- | ---- | ---- | ---- | ---- |
| **Касса** (4,64 млрд; банк / нақд; ↑ кечагига) | 1C, every class-5000 account with a balance | ⚠ | «банк» adds more than the bank: 5110.1, 5110.2 (расчётные), **5530.1** (прочие специальные), **5720** (продажи по картам — money not yet credited) and **5710** (переводы в пути, negative). The day's change moves with money in transit, not only the bank. ✋ Accounting decides what «касса / банк» is; proposal: 50xx + 51xx + 52xx as the figure, 55xx / 57xx shown apart as «махсус / йўлда». | 1C: ОСВ по счетам 50, 51, 52, 55, 57 на дату |
| **Кечаги сотув** ($40.16, 1 ҳисоб-фактура) | SAP `sales` | ⚠ | Invoices **dated 14.10 and entered by 08:00**, summed in **USD**. The shop's 14.10 sales entered on 15.10 aren't in it. Proposal: so'm (`DocTotalSy`), and «SAP'га киритилгани» in the label. | SAP: Продажи → Счета клиентов, дата 14.10 |
| **Захира** («ўқилмади») | SAP `stock_value` | ❌ → 🔧 | Every warehouse value 0 (the gateway's price). The brief rightly refused to show $0. Fixed in the gateway; shows after it's deployed. | SAP: Отчёты → Склад → «Отчёт о складских запасах» по складам |
| **Мижоз қарзи** («камида» 256,86 млн; «муддати ўтгани» = all, 44; 8 > 90 кун) | SAP `ar_open` | ⚠ → 🔧 | **The total is right** — the same as SAP's complete list of 44 open invoices (corrected 15.10, after the accountant's test; this page first said it was wrong). **«камида» was wrong**: the old duplicate push of 07:47 had marked the list "capped" (🔧 ignored now). **«муддати ўтгани» = all 44** because **due date = invoice date** in SAP (no payment terms), so every unpaid invoice counts as overdue from day one. The 8 over 90 days agree with the analyst. | SAP: Анализ сроков дебиторской задолженности на 15.10 (44 счета) |
| **Бугунги тўловлар** («йўқ») | written permissions (EMJ-SOP-ADM-01) | ✅ | No approved payment request dated today. It only covers permissions, not SAP / bank. | OPS Manager: «рухсат сўровлари» |
| **Дўконлар** (14.10 GARMIN MINOR, 1 чек) | Billz | ✅ | Matches Billz for 14.10. | Billz: отчёт по магазинам за 14.10 |
| **Ҳисобот юбормаганлар** (7 / 11) | the bot's daily reports | ✅ | As recorded. People who don't report by agreement → switch their reports off (`/xodimlar` → 📝). | OPS Manager: «кеча ким ҳисобот юбормади?» |
| **Давомат** (1 келмади, 3 кечикди) | Verifix | ✅ data / ⚠ people | As in Verifix. Someone on «эркин график» must be ticked in `/grafik`, or shows as late. | Verifix: табель за 14.10 |

## 3. Also found while checking

- **Garmin bot leads**: none have ever reached us (`garmin_leads` empty, the
  webhook never called) — the Garmin bot isn't posting. ✋ check its
  `COMMAND_CENTER_URL` / `COMMAND_CENTER_SECRET` (= `GARMIN_LEADS_SECRET`).
- **Garmin complaints**: the analyst gave the 05.10 complaint the code of the
  14.10 test ticket — the codes were listed apart from the complaints. 🔧 each
  complaint line now carries its own code and state.
- **«Қайси чеклар SAP'га киритилмаган?»** couldn't be answered by cheque
  number for the Garmin manager. 🔧 `garmin_data` → `billz_sap` (the check's
  results: not entered, wrong amount, entered late, by cheque).

## 4. The Бухгалтер test (15.10, 17:29–17:39)

| Asked | Verdict | Note |
| ---- | ---- | ---- |
| Касса / банк, 5110, 5530, 5720, 5710 | ✅ | 1C live, by account; «касса ва банк» = 5010 + 5110 only (more correct than the brief's «Касса», which adds 55 / 57) |
| 6010, 4310, 6310, 4010, 6410, 94 | ✅ | by account and counterparty |
| 9010 revenue, 9110 cost | ✅ honest / ✋ wrong question | this company posts revenue to **9020.1** (goods) and **9030.1** (services), cost to **9120.1** — 9010 / 9110 are empty. 🔧 the tool and `knowledge.py` say so; an empty account now names the ones with rows |
| «Кредиторлик (6010)» vs the comparison | ✅ both | 6010 is so'm only; the comparison is 6010 + 6015 (currency) − 4310 − 4315 — different scopes, both stated |
| SAP supplier balances «0 та» | ❌ → 🔧 | the rows have no date; a period asked dropped all 61. Undated kinds now ignore the period. `BalanceSys` now labelled so'm |
| 1C ↔ SAP comparison + both `.xlsx` | ✅ / 🔧 | files right (Uzbek Cyrillic + Russian, column notes at the bottom). INSPIRED = SAP's PAYME (same ИНН — right). 🔧 'YaTT "UMAROVA …"' and 'Tintolav' now pair by name (33 → 35) |
| Billz: yesterday, month vs SAP | ✅ | Billz 14.10 = one cheque at GARMIN MINOR; not in SAP yet |
| «Қайси чеклар SAP'га киритилмаган?» | ❌ → 🔧 | accounting had no cheque-level tool; 🔧 `billz_sap_check` for Billz + SAP |
| Attendance / KPI / overdue tasks | ✅ refused / 🔧 wording | it said the admin could open them — they are the Director's only; 🔧 the prompt says so |
| First question saved as a report | — | the bot asked «ҳа, ҳисобот / йўқ, бу савол» and «ҳа, ҳисобот» was pressed |

## 5. What the database keeps from SAP, 1C, Billz and Verifix (checked 15.10, for a clean-up)

The owner asked to delete the platforms' data that comes back by itself. Counts on 15.10:

| Table | What | Rows | Comes back by itself? | Delete? |
| ---- | ---- | ---- | ---- | ---- |
| `sap_gateway_snapshots` | SAP pushes (10 kinds), one copy per day since 23.08 | 20 640, 44 days | ✅ today's state at the next push (every 30 min; the gateway pushed every hour of the last 72, nights too). Old days: no feature reads them — everything reads `v_sap_gateway_latest` | ✅ safe |
| `ar_aging_snapshots` | SAP open invoices, one copy per day since 23.08 | 1 190, 44 days | ✅ at the next complete push (:13 / :43). Old days: nothing reads them — everything reads `v_ar_aging_latest` | ✅ safe |
| 1C, Billz, Verifix | read live on every question / report | — | nothing stored | — |
| `billz_sap_checks` | Billz → SAP check results, one per day | 13 (02–14.10) | ❌ only today's is made each morning; the Garmin brief and «which cheques» read the last 7 | keep |
| `daily_briefs` | every morning brief (its figures) | 71 (19.08–15.10) | ❌ the brief's «↑ кечагига» and the 14-day trend read it | keep |
| `alerts` | receivables alerts | 107 | ❌ history the Director's questions read | keep |
| `attendance_flexible` | people marked «эркин график» by Verifix id | 2 | ❌ the admin's settings | keep |
| `cash_balance_snapshots`, `attendance_snapshots`, `sales_summary_snapshots` | old, unused | 0 | — | empty already |

How (the owner runs it — direct writes need the Director's written permission, `docs/access-review-2026-10-07.md` §3):
a minute before a complete push (:12 or :42), in one transaction
`DELETE FROM sap_gateway_snapshots; DELETE FROM ar_aging_snapshots;`, then check `/db` → 🔒 the table → «Тури
бўйича»: every kind back with today's date after :13 / :43.

## 6. To do

| Who | What |
| ---- | ---- |
| ✋ IT (gateway computer) | delete the **old duplicate push task** (runs at :17 / :47); copy `sap-gateway/src/` and restart the gateway (stock value) |
| ✋ Accounting (Зайнаб) | which 1C accounts are «касса» and «банк» in the brief; whether «Кечаги сотув» should be in so'm and by entry date |
| ✋ SAP owner | payment terms on customers (so «муддати ўтган» means overdue, not just unpaid) |
| code (next) | sales in so'm + label; keep stock item rows for item questions; the cash breakdown once accounting decides |
