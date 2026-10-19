# 25 — Old stock (2026-10-17)

**Asked:** the owner — warn the Garmin manager and the Director about goods
getting close to a year in the warehouse, or past it.

**Code:** `integrations/org_bot/old_stock.py`, `agents/old-stock/agent.py`
(Mondays 08:00, in the morning job), template `old_stock.html` (PDF).

**From SAP's push:** every item in stock per warehouse (`stock_items`), each
item's last purchase date (`products`, OITM.LastPurDat), and what sold in 90
days (`item_sales_90d`).

- **a year or more** — last bought 365+ days ago: every unit on hand is at
  least that old (exact);
- **approaching** — 335–364 days;
- an item bought again since may still hold older units — not visible (SAP's
  batch history isn't pushed); the PDF says so.

**Who:** the Director every warehouse; the Garmin manager the Garmin warehouses —
the shops' (`BILLZ_SAP_WAREHOUSES`: 05 Garmin-Minor, G.A._01 Garmin Abay, G.A._02
Garmin Tanita) and, since 19.10.2026, every SAP warehouse named «Garmin»
(`garmin_codes`: 09 zahiralar, 10 Ambasador, 03 Bodomzor, 04 Abay, 06 faulty).
A caption (rows, value, how many sold nothing in 90 days) + the PDF list, biggest
value first, red «0» where nothing sold in 90 days. **/eski** (their ☰ menus) any time.
Once a week (`agent_actions` `old_stock_weekly`); `--force`, `--dry-run`.

First run on 17.10.2026: all warehouses — 428 rows a year or more ($148 361), 2
approaching; Garmin warehouses — 208 and 0.

**Display dummies left out** (2026-10-17): «Dummy Unit…» and codes like 010-M2704-00 aren't stock for sale. Many items share a last-purchase date (22–23.04.2025, 21.05.2025) — a big shipment or the first stock load into SAP; they are at least that old. After this: all 318 rows, Garmin 137.
