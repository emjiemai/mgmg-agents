# 21 — Supplier debt: 1C against SAP B1 (2026-10-09)

**Asked:** the Director's task of 08.10.2026 — «1C билан SAP B1даги
кредиторлик қарздорлигини солиштириб чиқинг… нега фарқлар бор?», deadline 09.10.
**Access:** IT may not see payables (the order of 07.10); the Director gave
written temporary permission on 09.10 for this task (`docs/access-review-2026-10-07.md`,
section 4). Everything is read only.
**Code:** `scripts/ap_reconcile.py` (no data in the repository).

## What it compares

- **1C** (OData, GET only): balances on 6010 / 6015 (payables to suppliers and
  contractors, so'm and currency — credit balance = we owe) and 4310 / 4315
  (advances paid — debit). Per counterparty: debt, advance, net = debt − advance;
  per contract in the detail sheet. Persons (`ФизическиеЛица`, e.g. 6970
  подотчетные) are left out.
- **SAP B1**: suppliers' balances (OCRD, CardType S) — from the gateway tool
  `get_supplier_balances` (`docs/sap-gateway-tools.md`, tool 4) or, until it
  exists, an Excel export of the same. Compared in so'm (`BalanceSys`).

Matching: ИНН first, then the cleaned-up name (Latin → Cyrillic, legal forms
and quotes dropped). Equal within 1,000 so'm or 0.5 % = «Мос».

## The likely reasons it names

Debt and an advance on the same supplier in 1C at once (the advance not
offset — зачет аванса; SAP shows one net balance) · advance counted in one system only · debt in one system, overpayment in the
other · currency debt (6015) revalued at different rates · SAP amount only in
USD · only in 1C / only in SAP (document not entered, or the supplier under
another name) · otherwise: a document or payment differs — акт-сверка. A
reason is a hint for the accountant, never a verdict.

## Output

One Excel workbook — Солиштириш, Фақат 1C, Фақат SAP, 1C тафсилот, Изоҳ —
for the Director and accounting. The script prints and logs counts only.

## Found on 09.10.2026 (1C side, read 09.10)

Counts only (the figures are in the workbook for the Director): 67 counterparties
with a balance on 6010/6015/4310/4315 — 9 with debt only, 50 with an advance
only, **6 with both at once** (advance likely not offset), 2 with a currency
debt (6015, USD and EUR); 5 have no ИНН (matched by name). The account totals
in the workbook equal 1C's own account balances.

The SAP side: tool 4 `get_supplier_balances` is written into the gateway
(`sap-gateway/src/supplier-balances.js`, 2026-10-09) and the push script
sends it as `supplier_balances`; once the gateway is updated, run with
`--sap-pushed` instead of `--sap file.xlsx` (needs `DATABASE_URL`):
`python scripts/ap_reconcile.py --onec-json 1c.json --sap-pushed --out solishtirish.xlsx`.

## Tests

`scripts/selfcheck.py`, `test_ap_reconcile`: 1C netting, persons left out,
currency kept, SAP export headers found, sign turned, ИНН and name matching,
reasons, the workbook's sheets, nothing written anywhere.

## SAP's sign (fixed 2026-10-09)

SAP stores a supplier advance as a negative balance. The first run guessed
the sign from the majority of balances and turned every advance into a
debt, doubling each "difference" (the exchange's 32,419,520.78 advance, equal
to the tiyin in both systems, showed as 64.8 mln). The sign is now chosen by
agreement with 1C on the suppliers found in both (`choose_sign`): on the
09.10 data 32 matched, 15 agree, 17 differ.
