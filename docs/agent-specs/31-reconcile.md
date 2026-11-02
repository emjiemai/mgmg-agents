# SAP B1 ↔ 1C ↔ Billz discrepancy report for accounting (2026-11-02)

**Asked by:** the Boss, task T-2026-11-0048 («Ҳар куни соат 9:00 да Didox, SAP, 1C ва Billz маълумотларини солиштириб,
барча фарқлар, хатолар ва номутаносибликлар бўйича ҳисоботни … Зайнабхонга юборишни йўлга қўйинг»); the owner the same
day: she gets the wrong data too, not only the differences; SAP ↔ 1C ↔ Billz now, AmoCRM and Didox later.
**Code:** `integrations/reconcile/core.py` (pure: matching, balances, the shop, wrong data), `integrations/reconcile/run.py`
(reading, the workbook, sending). **When:** 09:00 on working days (Mon–Sat, not a /dam day off), in the API's
5-minute round, once (`reconcile_runs`); **/farqlar** (/сверка) any time. **Who:** active Бухгалтерия people who hold
SAP and 1C on `/xodimlar` (today Зайнабхон); /farqlar also the Director.

## What is compared

| Section | SAP B1 (pushed by the gateway) | 1C (OData, GET) | Window |
| ---- | ---- | ---- | ---- |
| Sales documents | A/R invoices and credit notes (`sales`), **without the shop's** (warehouses of `BILLZ_SAP_WAREHOUSES`) | «Реализация товаров и услуг» | last 14 days (what the push sends complete) |
| Money in | incoming payments (`payments`), **without the shop's customers** (per cheque in SAP) | bank receipts «Оплата покупателя», «Возврат от поставщика»; ПКО | last 30 days |
| Money out | outgoing payments (`payments_out`) | bank payments «Оплата поставщику», «Возврат покупателю»; РКО | last 30 days |
| Customers' debt | customer balance (`BalanceSys`, so'm) | 4010 + 4015 − 6310 + 6315, by counterparty | now |
| Suppliers' debt | supplier balance | 6010 + 6015 − 4310 + 4315 (`onec/payables.py`, as before) | now |
| The shop, day by day | shop invoices (G.A._01, 05, G.A._02) | «Отчёт о розничных продажах» | last 7 days, + Billz net sales |

Documents are paired by counterparty (ИНН, else the cleaned-up name), amount (≤ 1 000 сўм or 0.5 %) and date (± 3
days): **мос**, **санаси бошқа**, **сумма фарқ**, **фақат SAP'да**, **фақат 1C'да** (problems first). Left out on
purpose (checked live 02.11): 1C's card-terminal receipts (83 of 163 bank receipts — the shop's money), bank fees,
salaries, taxes and transfers between our own accounts (SAP doesn't record them); the shop's 30 retail customers.

**Wrong data** (each record named): SAP — customer without ИНН (with a balance or in use) or with a malformed one, one
ИНН on two cards, an invoice without a salesperson, a zero price, a date in the future, an invoice entered more than 3
days after its date, a payment without a counterparty, stock below zero. 1C — a counterparty in use without ИНН or
with a malformed one, one ИНН twice, a document of the period not posted.

## What she gets

One chat message (Uzbek, fixed text): a count per section, an unread system named («Ўқилмади: 1C» — never shown as a
zero). And one Excel workbook in her language (`employees.lang`: Russian for Зайнабхон, Uzbek otherwise): Сводка /
Хулоса, Продажи, Поступления, Списания, Дебиторка, Кредиторка, Магазин, Ошибки в данных — every row, problems first
and red, filters on. Amounts in so'm (SAP system currency; 1C at the document's rate).

First live run (02.11, counts only): sales 28 to look at, money in 39, money out 37, customers 38, suppliers 66, the
shop 7 days (1C has almost no retail reports), wrong data 70.

**Next:** AmoCRM and Didox when connected (the owner, 02.11). Logs: counts only; IT never gets the figures.
