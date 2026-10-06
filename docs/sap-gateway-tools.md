# SAP gateway — three tools the Command Center needs

For: whoever maintains the local SAP gateway (Node.js, `localhost:3000`,
`SAP_B1_AI_AGENT_TEACHING_UPDATED.md`).
From: the MGMG Command Center (`push-ar-aging.ps1` pushes gateway results to
`mgmg-api`), 2026-10-03.
**Status:** added to the gateway by Abdulbosit on 2026-10-03 (the owner's word;
`push-ar-aging.ps1 -Check` shows each tool's row count).

## Why

The gateway's tools return at most the **latest 100 rows**, with no date
filter. That's right for a chat question, but the morning report needs
complete numbers. On the SAP export of 2026-10-02:

| What the Director gets | From today's tools | Real |
| ---------------------- | ------------------ | ---- |
| Customer debt | "at least" $6,549, 19 invoices | **$33,971 open, 52 invoices** (oldest due 29.01.2026), 10 partly paid — `get_invoices` has no `PaidToDate` and only the latest 100 invoices |
| Stock value | not readable (100 of ~1,050 stock rows) | **$513,345** |
| Yesterday's sales | from orders | sales are invoices (on 02.10: 2 invoices, 0 orders) |
| Billz → SAP check | impossible: no invoice lines, no warehouse, no so'm total, no entry date | — |

The Director also asked for a daily check that every cheque rung up in the
Garmin shop's BILLZ till reaches SAP the same day — late entries have cost
the business money. That needs yesterday's (and the last two weeks')
invoices **with their lines**.

## The three tools

All three follow the teaching file's "Future Tool Design Standard": fixed SQL
templates, strict input validation, bounded results, read-only, minimal
columns, the same Bearer token. Column names were checked against the
2026-10-02 export (`scripts/sap-gateway-push/sap_columns.json`;
`scripts/selfcheck.py` re-checks the SQL below on every change). Schema
`"MGM"` is the company database — use whatever the gateway already uses.

### 1. `get_open_invoices` — customer debt (A/R aging)

Every open, non-cancelled A/R invoice with what has been paid so far.

Input: `{}` or `{"limit": 1..1000}` (default 1000).
Rows today: **52**.

```sql
SELECT T0."DocEntry", T0."DocNum", T0."CardCode", T0."CardName",
       TO_VARCHAR(T0."DocDate", 'YYYY-MM-DD') AS "DocDate",
       TO_VARCHAR(T0."DocDueDate", 'YYYY-MM-DD') AS "DocDueDate",
       T0."DocStatus", T0."CANCELED", T0."DocCur", T0."DocTotal", T0."PaidToDate",
       T0."DocTotalFC", T0."PaidFC", T0."SlpCode", T1."SlpName"
FROM "MGM"."OINV" T0
LEFT JOIN "MGM"."OSLP" T1 ON T1."SlpCode" = T0."SlpCode"
WHERE T0."DocStatus" = 'O' AND T0."CANCELED" = 'N'
ORDER BY T0."DocDueDate"
LIMIT ?
```

### 2. `get_sales_by_date` — invoice and credit-note lines in a date range

One row per document line of A/R invoices (`ObjType` 13) and A/R credit
notes (14) **dated or entered** in the range — "entered" so a sale typed in
days later is still seen (the Billz check reports such late entries).

Input: `{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}`. Validate: real dates,
`from <= to`, range at most **31 days**.
Rows: about 10–15 lines a day; the script asks for 14 days (~150–200 rows).

```sql
SELECT 13 AS "ObjType", T0."DocEntry", T0."DocNum", T0."CardCode", T0."CardName",
       TO_VARCHAR(T0."DocDate", 'YYYY-MM-DD') AS "DocDate",
       TO_VARCHAR(T0."CreateDate", 'YYYY-MM-DD') AS "CreateDate", T0."CreateTS",
       T0."CANCELED", T0."DocCur", T0."DocTotal", T0."DocTotalFC", T0."DocTotalSy",
       T0."SlpCode", T0."UserSign",
       T1."LineNum", T1."ItemCode", T1."Dscription", T1."Quantity", T1."WhsCode",
       T1."CodeBars", T1."LineTotal"
FROM "MGM"."OINV" T0
INNER JOIN "MGM"."INV1" T1 ON T1."DocEntry" = T0."DocEntry"
WHERE T0."DocDate" BETWEEN ? AND ? OR T0."CreateDate" BETWEEN ? AND ?
UNION ALL
SELECT 14 AS "ObjType", T2."DocEntry", T2."DocNum", T2."CardCode", T2."CardName",
       TO_VARCHAR(T2."DocDate", 'YYYY-MM-DD') AS "DocDate",
       TO_VARCHAR(T2."CreateDate", 'YYYY-MM-DD') AS "CreateDate", T2."CreateTS",
       T2."CANCELED", T2."DocCur", T2."DocTotal", T2."DocTotalFC", T2."DocTotalSy",
       T2."SlpCode", T2."UserSign",
       T3."LineNum", T3."ItemCode", T3."Dscription", T3."Quantity", T3."WhsCode",
       T3."CodeBars", T3."LineTotal"
FROM "MGM"."ORIN" T2
INNER JOIN "MGM"."RIN1" T3 ON T3."DocEntry" = T2."DocEntry"
WHERE T2."DocDate" BETWEEN ? AND ? OR T2."CreateDate" BETWEEN ? AND ?
```

Parameters, in order: `from, to, from, to, from, to, from, to`.
`DocTotalSy` is the total in SAP's system currency (so'm) — the BILLZ tills
count in so'm. `CreateTS` is the entry time (hhmmss).

### 3. `get_stock_value` — stock value per warehouse

A summary, not item rows: one line per warehouse.

Input: `{}`.
Rows today: **≤ 20**.

```sql
SELECT T0."WhsCode", T1."WhsName", COUNT(*) AS "Items",
       SUM(T0."OnHand") AS "OnHand", SUM(T0."StockValue") AS "StockValue"
FROM "MGM"."OITW" T0
INNER JOIN "MGM"."OWHS" T1 ON T1."WhsCode" = T0."WhsCode"
WHERE T0."OnHand" <> 0
GROUP BY T0."WhsCode", T1."WhsName"
ORDER BY T0."WhsCode"
```

## As built vs this spec — checked against the database 2026-10-06

For the gateway's maintainer. The pushes of 03.10 show the tools were added,
but not as specified:

| Tool | What arrives | What's missing / different | Effect |
| ---- | ------------ | -------------------------- | ------ |
| `get_sales_by_date` | 101 invoice **lines**, 19.09–02.10 (not the 14 days asked) | `ObjType`, `CreateDate`, `CreateTS`, `DocTotalFC`, `DocTotalSy`, `UserSign`, `Dscription`, `CodeBars`; credit notes (ORIN) not included; looks capped at ~100 rows | no so'm total, no "entered late" check, older days cut off |
| `get_stock_value` | 1,035 **item** rows (`ItemCode`, `ItemName`, `AvgPrice`, `OnHand`, `StockValue`) instead of ≤ 20 warehouse totals | `Items` | the receiver kept one item per warehouse → stock read as $0. **Fixed on our side:** item rows are now summed per warehouse on arrival (`push_handler.stock_by_warehouse`) |
| `get_open_invoices` | 50 open invoices with `PaidToDate` ✓ | the so'm amounts `DocTotalFC` / `PaidFC` — unknown whether they're sent (the pushes of 03.10 predate the missing-columns log); no seller on the biggest invoices | debt shown in SAP's local currency (USD) while every invoice is written in so'm (`DocCur` = UZS). **Ready on our side (2026-10-06):** `DocCur`/`DocTotalFC`/`PaidFC` are stored and shown as so'm the moment they arrive; the next push's `missing_columns` says whether they did |

**Currency, for everyone reading SAP figures:** SAP's local currency here is
USD (`DocTotal` of a 9.4 mln so'm hotel invoice is 739.79). The invoices are
written in so'm, so the Director, opening invoice 2150 in SAP, sees so'm,
while the bot said "$9,764.31". Every document pushed so far (orders, sales,
payments, customers) has `DocCur` = UZS. Until `DocTotalFC`/`PaidFC` arrive,
every SAP amount is labelled as the USD equivalent; once they do, the debt
is shown in so'm as written on each invoice (`ar_aging_snapshots.doc_currency`,
`doc_total_fc_tiyin`, `paid_fc_tiyin`). A part-paid invoice without `PaidFC`
keeps the USD figure — its so'm balance isn't guessed.

**Payment terms:** 44 of the 50 open invoices have `DocDueDate` = `DocDate`,
so all of them are "overdue" from the first day. That's SAP's data (no
payment terms on the customers), not a calculation error.

**The push itself stopped** on 03.10 at 14:13 and hasn't resumed (06.10),
as before on 18.09 (10:47 → 19.09 10:17) and 24.09 (10:17 → 30.09 09:17):
the computer running the scheduled task is off, asleep or offline. The brief
now says so (see `01-ceo-daily-brief.md`, "SAP gone quiet").

## Response shape

The same as the existing tools: `{"ok": true, "data": [ {...}, ... ]}`, or
`{"ok": false, "error": "..."}`. Column names exactly as in the SQL.

## How they are used

`push-ar-aging.ps1` (on the gateway machine, every 30 minutes) calls each
tool with the gateway's Bearer token and posts `data` to
`https://mgmg-api-eeky.onrender.com/webhooks/sap-data/<kind>/<secret>`.
Until a tool exists, the script skips it and keeps using today's tools.

| Tool | Command Center kind | Used by |
| ---- | ------------------- | ------- |
| `get_open_invoices` | `ar_open` | brief «Мижоз қарзи», receivables alert, cash calendar |
| `get_sales_by_date` | `sales`, `sales_lines` | brief «Кечаги сотув», Billz → SAP check |
| `get_stock_value` | `stock_value` | brief «Захира» |

Nothing else changes: the gateway stays loopback-only, the token and HANA
credentials stay in its `.env`, and the script sends only these results.

## Later (not needed now)

For agents still waiting in the owner's plan: customer equipment cards
(OINS) and open service calls (OSCL) for service reminders; open purchase
invoices (OPCH) by due date for the cash calendar; slow-moving stock (OITW +
OITM.LastPurDat). Each would be its own small tool, specified the same way.
