# Every link the system uses (2026-10-10)

For the IT book (the owner's instruction of 08.10.2026, IT-7). **No secret is
written here**: `{…_SECRET}` parts of a webhook link live only in Render
(`mgmg-shared`, `sync: false`) — never paste them into chat, docs or tickets.

Base address of the service: **https://mgmg-api-eeky.onrender.com** (Render
`mgmg-api`; no company domain — decided).

## 1. Public pages — open for anyone (QR codes point here)

| Link | What | Note |
| ---- | ---- | ---- |
| `/f` | Londry complaint page (Юнусобод / Вузгородок) | **printed on QR cards — never move it** |
| `/f/garmin` | Garmin complaint page (Абай / Минор) | |
| `/f?branch=yunusobod` · `vuzgorodok` · `/f/garmin?branch=abay` · `minor` | the same page with the branch already chosen | |
| `/r` | Londry Google-review page (green, five stars) | |
| `/r/garmin` | Garmin Google-review page | |
| `/r/go/yunusobod` · `vuzgorodok` · `abay` · `minor` | one tap → that branch's Google review page | needs `GOOGLE_REVIEW_URLS` (**not set yet**) |
| `?lang=ru` · `?lang=en` on any page above | Russian / English; default Uzbek Cyrillic | |
| `/health` | "is the service up" — for monitoring | no data |

## 2. Closed pages — password

| Link | What | Access |
| ---- | ---- | ---- |
| `/db` | read-only database viewer: technical tables only (Director's order of 07.10) | HTTP login, `DB_VIEWER_PASSWORD`; off when it isn't set |

## 2a. Backups — the office computer only (secret in the link)

| Link | What | Secret |
| ---- | ---- | ---- |
| `GET /backup/{secret}` | status for `pull-backup.ps1 -Check` (makes nothing) | `BACKUP_SECRET` |
| `POST /backup/{secret}` | make an encrypted backup; answers its manifest | `BACKUP_SECRET` |
| `GET /backup/{secret}/{file}` | fetch that encrypted file, once (gone after, or after an hour) | `BACKUP_SECRET` |

Wrong secret or not set up = "not found". The file only opens with the owner's private key.

## 3. Webhooks — machines only (each ends in a secret)

| Link | Who calls it | Secret (Render variable) |
| ---- | ---- | ---- |
| `/webhooks/telegram/admin/{secret}` | Telegram → Admin Bot | `ADMIN_BOT_WEBHOOK_SECRET` |
| `/webhooks/telegram/ops/{secret}` | Telegram → OPS Manager Bot | `OPS_MANAGER_BOT_WEBHOOK_SECRET` |
| `/webhooks/sap-data/{kind}/{secret}` | SAP gateway push script — complete data (ar_open, sales, sales_lines, stock_value, supplier_balances …) | `SAP_PUSH_WEBHOOK_SECRET` |
| `/webhooks/sap-gateway-push/{tool}/{secret}` | the same script — today's limited tools (get_orders, get_products …) | `SAP_PUSH_WEBHOOK_SECRET` |
| `/webhooks/sap-push/{secret}` | the same script — get_invoices (receivables) | `SAP_PUSH_WEBHOOK_SECRET` |
| `/webhooks/garmin-lead/{secret}` | the Garmin AI bot → its leads | `GARMIN_LEADS_SECRET` |

## 4. Inside the company network

| Link | What |
| ---- | ---- |
| `http://localhost:3000` (on the gateway computer) | the SAP gateway (`sap-gateway/`, Node.js) — only the push script talks to it; `/health`, `/tools/<name>` |
| `https://clobus.uz/a/acc313/61458/odata/standard.odata/` | 1C «Бухгалтерия для Узбекистана» on Clobus, OData (read-only user) — `ONEC_ODATA_URL` |

## 5. Outside services the system calls

| Service | Address | What for | Key (Render) |
| ---- | ---- | ---- | ---- |
| Telegram Bot API | `https://api.telegram.org` | both bots | the bot tokens |
| OpenRouter (AI) | `https://openrouter.ai/api/v1` | every AI answer | `OPENROUTER_API_KEY` |
| BILLZ | `https://api-admin.billz.ai` (docs: `https://docs.billz.io`) | Garmin shop tills | `BILLZ_SECRET_TOKEN` |
| Verifix | `https://app.verifix.com` | attendance | `VERIFIX_LOGIN` / `PASSWORD` / `FILIAL_ID` |
| Google Sheets | `https://sheets.googleapis.com` (login `https://oauth2.googleapis.com/token`) | the leads sheet "POSSIBLE Leads" | `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_LEADS_SHEET_ID` |
| SerpAPI | `https://serpapi.com/search` | Lead Agent web search | `SERPAPI_API_KEY` |
| Tavily | `https://api.tavily.com/search` | Lead Agent web search | `TAVILY_API_KEY` |
| World Bank | `https://search.worldbank.org/api/v3/projects` | Lead Agent tenders | — |
| Google reviews | `https://g.page/r/…` per branch | the review buttons | `GOOGLE_REVIEW_URLS` (**not set**) |

## 6. Where things are managed

| What | Where |
| ---- | ---- |
| Code (this system) | https://github.com/emjiemai/mgmg-agents |
| Garmin AI bot (separate) | https://github.com/emjiemai/Garmin-AI-bot · Telegram @garminofficialuzbot |
| Hosting, settings, logs, database | Render dashboard: services `mgmg-api`, crons `mgmg-morning-agents`, `mgmg-daily-reports`, `mgmg-report-reminder`, `mgmg-team-cheer`; database `mgmg-db`; settings group `mgmg-shared` |
| Bots' owner settings | Telegram @BotFather (Admin Bot, OPS Manager Bot) |
| SAP push task | gateway computer: Windows task "MGMG SAP push" (`scripts/sap-gateway-push/`) |
| Backup task | office computer: Windows task "MGMG database backup" (`scripts/backup/`) |
| PostgreSQL apt repository (the image's `pg_dump` 16) | https://apt.postgresql.org (key https://www.postgresql.org/media/keys/ACCC4CF8.asc) |
