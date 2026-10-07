# Billz → SAP check — did every shop cheque reach SAP, on time?

**Code:** `integrations/billz/sap_check.py` (the rules, the message),
`agents/billz-sap-check/agent.py` (reads both sides, sends)
**Runs:** 08:00 with the morning agents, right after the brief, for yesterday
**Stored in:** `billz_sap_checks` (one row per checked day: status, what was found)
**Switches:** `BILLZ_SAP_CHECK_ENABLED` (default on), `BILLZ_SAP_WAREHOUSES`,
`BILLZ_SAP_CHECK_ROLES`
**Needs:** BILLZ (`BILLZ_SECRET_TOKEN`) and SAP's invoice lines from the
gateway tool **`get_sales_by_date`** (`docs/sap-gateway-tools.md`; added by
the gateway's maintainer 2026-10-03) — the older capped tools carry no lines,
warehouse or so'm total. Until its data has arrived once, the agent exits
quietly.

## Why (the owner, 2026-10-03)

A watch sold in the Garmin shop is rung up in the BILLZ till; at the end of
the day the shop enters the same sale into SAP. When that's late or
forgotten, SAP's stock, sales and debts are wrong — the Director says it has
cost thousands of dollars. People make mistakes, so the system checks.

## What SAP looks like (the export of 2026-10-02)

- One **A/R invoice per cheque** (OINV), mostly to the customer "B2C клиенты",
  written in so'm.
- From the warehouses **G.A._01** (Garmin Abay), **05** (Garmin-Minor) and,
  for the Tanita scales the shop also sells, **G.A._02** (Garmin Tanita).
- Entered by the shop's own SAP user, "Гармин (филиал Абай)", usually
  18:00–20:00 the same day — and sometimes later: 20.09's sales were entered
  on 24.09, 29.09's on 30.09.

## How a cheque is matched

- **A cheque** = one BILLZ order (`product-general-table` by position, one
  day at a time, every shop): its lines' `net_sales` added up — so'm, after
  discounts; a return is negative.
- **An SAP document** = a non-cancelled A/R invoice (a credit note counts
  negative) with a line in the shop's warehouses. Its so'm amount is
  `DocTotalSy` (SAP's system currency is so'm; equal to `DocTotalFC` on every
  so'm invoice in the export).
- **Same amount** (± 1,000 so'm or 0.2%) pairs them — a shared product code
  first (BILLZ's SKU is SAP's item code; the barcode too), then the nearest
  date. "Date" is the document's date **or the day it was entered**, within
  7 days: the shop sometimes types an old date (22.09's sale entered on 22.09
  under 07.09; 30.09's under 25.09).
- If the gateway sends no so'm total for a document, its amount is
  "unknown", not 0: it pairs by shared product and date only, and the
  message says the amounts didn't come (the first test run, 2026-10-03).
  Checked on the 2026-10-02 export: 52 shop sales, all paired right, with and
  without so'm totals.
- Left over, same day, same product, different amount → **"суммаси фарқ
  қилади"**.
- A sale and its later return that both never reached SAP cancel out.

## What the Director gets

All matched — one line:

    🧾 Billz ↔ SAP — 02.10.2026
    ✅ Кечаги 5 та чекнинг ҳаммаси SAP'га киритилган (23,54 млн сўм).

Otherwise:

    🧾 Billz ↔ SAP — 02.10.2026
    Billz: 5 та чек — 23,54 млн сўм
    SAP: 3 та ҳужжат — 14,2 млн сўм

    🔴 SAP'га киритилмаган — 2 та, 9,34 млн сўм:
       • 02.10 · чек 1234 · 8,7 млн сўм — Venu 4 (41mm) · сотувчи Абдурашид
    🟡 Суммаси фарқ қилади — 1 та:
       • 02.10 · чек 1236: Billz 3,3 млн сўм, SAP 3 млн сўм (№2411)
    🟠 Кечикиб киритилган — 1 та:
       • 29.09 сотуви 30.09 куни киритилди (№2404, 9,5 млн сўм)
    ⚪ SAP'да бор, Billz'да йўқ — 1 та:
       • №2410 · 3,3 млн сўм · Sherzod Ganiyev

- **Not entered** cheques of the last 14 days are repeated every morning
  until they reach SAP.
- **Late** entries, and entries made on time under another date, are told
  once — the morning after they were entered.
- **In SAP, not in Billz** — only yesterday's documents.
- When SAP's last push was before midnight, a line says entries made after
  it aren't seen. When SAP's data is more than a day old: "SAP маълумоти …
  дан бери янгиланмаган — солиштирилмади", nothing else.

Who: the Director; more roles with `BILLZ_SAP_CHECK_ROLES` (e.g.
`garmin_sotuv` so the shop sees what it must enter).

When something is wrong the check is a **PDF** (tables per section) with a
one-line caption; "all entered" and "SAP data too old" stay one text line
(`18-reports.md`, 2026-10-05).

**Trial first** (`BILLZ_SAP_CHECK_TRIAL`, on by default): BILLZ's per-cheque
report hasn't been seen with real data yet, so until the admin confirms the
first mornings match the shop, the message goes to the admin in Admin Bot
("🧪 Синов…"), not the Director. `BILLZ_SAP_CHECK_TRIAL=false` in Render
switches it over. Since the Director's order of 07.10.2026 (IT sees no
figures) the admin's trial copy is **counts only** (`sap_check.technical_text`:
how many cheques and documents, how many not entered / wrong amount / late /
extra) — no amounts, cheque numbers, shops, sellers or customers, and no PDF.
So the trial is confirmed by the Director or the specialist he names, not by IT.

If BILLZ ever gives lines without a cheque id, no cheque can be named:
only yesterday's totals are compared ("фақат жами солиштирилди"), never a
false "not entered" list.

## Settings

- `BILLZ_SAP_WAREHOUSES` (default `G.A._01,05,G.A._02`): the SAP warehouses the shops
  sell from — one list for every shop, or per shop by its BILLZ name:
  `GARMIN ABAY=G.A._01,05;GARMIN MALIKA=21`. Lists must not overlap.
- If warehouse 05 turns out to carry sales that never go through BILLZ, they
  show up under "SAP'да бор, Billz'да йўқ" — then set the list to `G.A._01`.

## Checks

`selfcheck.py` (`test_billz_sap_check`, `test_sap_full_push`): grouping lines
into cheques; cancelled and other-warehouse documents left out; credit notes
negative; matching, tolerance, amount differs, late entry told once, extra
only for yesterday, sale + return cancel out; per-shop warehouses; the
message (✅ line, sections, Uzbek Cyrillic); stale SAP; the BILLZ request.
Checked on the real SAP export too: it finds the 29.09 → 30.09 late entries.

## Runbook

```bash
python agents/billz-sap-check/agent.py --dry-run   # print, send nothing, store nothing
python agents/billz-sap-check/agent.py --force     # send again today
```
