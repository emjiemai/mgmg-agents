# BILLZ — shop sales (read-only)

**Code:** `integrations/billz/client.py` (the API), `integrations/billz/sales.py`
(what is shown); wired into the 08:00 brief, OPS Manager Bot (`billz_savdo`)
and Admin Bot (`/billz`)
**Switch:** `BILLZ_SECRET_TOKEN` (empty = everything Billz is hidden);
`BILLZ_ENABLED=false` hides it again
**Mode:** read-only — no sale, transfer or catalogue change is ever sent
**Also:** the 08:00 Billz → SAP check, cheque by cheque (`17-billz-sap-check.md`)

## What the Director sees

In the morning brief, under the five numbers:

    🛍 Дўконлар (Billz), 30.09.2026: 12,4 млн сўм · 34 та чек
       • Garmin Next — 8,1 млн сўм (20 та чек, 1 та қайтариш)
       • Garmin Samarqand Darvoza — 4,3 млн сўм (14 та чек)

"Сотув" is BILLZ's `net_gross_sales`: revenue after discounts and returns.
And any time in OPS Manager Bot: "do'konlarda savdo qanday?", "kecha
magazinda qancha sotildi?", "qaysi sotuvchi ko'p sotdi?" — last 30 days per
shop per day, per seller, and the top products.

## Setting it up

1. BILLZ: **Настройки → Компания → Ключи интеграции → Новый ключ** (name it
   e.g. "MGMG Command Center"), copy the key.
2. Render → Environment Groups → `mgmg-shared`: `BILLZ_SECRET_TOKEN=<key>`.
3. Admin Bot: `/billz` → "✅ Billz уланди. Дўконлар: N та" and yesterday's sales.

The key's default role is enough: every report used here is in the key's
default route list. Only cost price (`supply_price`) needs an extra role
switch, and nothing here uses it.

## API facts this relies on

Source: https://docs.billz.io and its OpenAPI spec (read 2026-10-01).

- `POST https://api-admin.billz.ai/v1/auth/login` `{"secret_token": key}` →
  `data.access_token`, valid 15 days. A 401 later = log in again once. The
  refresh token is not used: reusing a spent one revokes every session of
  the key, and each cron run is a new process anyway.
- `GET /v1/shop`, `/v1/general-report-table` (shop × day, `detalization`
  required), `/v1/seller-general-table`, `/v1/product-general-table`
  (`shop_ids` required). Arrays comma-separated; dates `YYYY-MM-DD` in
  Tashkent time; amounts in whole so'm; `page` + `limit`, total in `count`.
- 2 requests a second per IP: pages are 0.6 s apart and a 429 is retried
  with backoff.
