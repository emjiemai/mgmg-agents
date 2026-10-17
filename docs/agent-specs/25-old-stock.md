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

**Who:** the Director every warehouse; the Garmin manager the Garmin warehouses
(`BILLZ_SAP_WAREHOUSES`: 05 Garmin-Minor, G.A._01 Garmin Abay, G.A._02 Garmin Tanita).
A caption (rows, value, how many sold nothing in 90 days) + the PDF list, biggest
value first, red «0» where nothing sold in 90 days. **/eski** (their ☰ menus) any time.
Once a week (`agent_actions` `old_stock_weekly`); `--force`, `--dry-run`.

First run on 17.10.2026: all warehouses — 428 rows a year or more ($148 361), 2
approaching; Garmin warehouses — 208 and 0.
