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
| `ar_open` → `ar_aging_snapshots` | OINV, open (DocStatus O, not cancelled) | `get_open_invoices` | 44 invoices | ✅ complete | brief «Мижоз қарзи», receivables alert, cash calendar, `sap_receivables` | ❌ **overwritten every :17 / :47** by the old `get_invoices` (100 rows, no PaidToDate — every invoice at its full total) from an **old duplicate Windows task**. 🔧 now ignored when the complete list is newer; ✋ delete the old task. |
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
| **Мижоз қарзи** («камида» 256,86 млн; «муддати ўтгани» = all, 44; 8 > 90 кун) | SAP `ar_open` | ❌ → 🔧 | Read at 08:01 from the old capped push of 07:47 (the duplicate task) — hence «камида» and full totals. Also **due date = invoice date** in SAP (no payment terms), so every unpaid invoice counts as overdue from day one. | SAP: Анализ сроков дебиторской задолженности на 15.10 (44 счета) |
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

## 4. To do

| Who | What |
| ---- | ---- |
| ✋ IT (gateway computer) | delete the **old duplicate push task** (runs at :17 / :47); copy `sap-gateway/src/` and restart the gateway (stock value) |
| ✋ Accounting (Зайнаб) | which 1C accounts are «касса» and «банк» in the brief; whether «Кечаги сотув» should be in so'm and by entry date |
| ✋ SAP owner | payment terms on customers (so «муддати ўтган» means overdue, not just unpaid) |
| code (next) | sales in so'm + label; keep stock item rows for item questions; the cash breakdown once accounting decides |
