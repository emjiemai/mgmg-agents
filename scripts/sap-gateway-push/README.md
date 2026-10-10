# SAP push script

`push-ar-aging.ps1` runs on the machine with the SAP gateway (the local
Node.js service, `localhost:3000`, `SAP_B1_AI_AGENT_TEACHING_UPDATED.md`),
calls the gateway's tools and sends their answers to the Command Center
(`mgmg-api`), which keeps them for the morning brief, the receivables
alert, the Billz → SAP check and the Director's questions.

It follows the gateway's rules: no SQL and no database password here — the
gateway is the only thing that talks to SAP's HANA database. Plain Windows
PowerShell 5.1, nothing to install, only outbound connections.

## What it sends

**Today's tools** (exist now) — at most 100 rows each, so the brief shows
those numbers as "камида":
`get_invoices`, `get_orders`, `get_products` (20), `get_customers`,
`get_warehouses`, `get_inventory`, `get_payments`.

**Four complete tools** — specified in `docs/sap-gateway-tools.md`; their
code is in `sap-gateway/src/` (this repo). The script tries them on every run; one that
isn't there yet is skipped with "not in the gateway yet", and is used from
the first run after it appears — nothing to change in the script:

| Gateway tool | Becomes | Makes complete |
| ------------ | ------- | -------------- |
| `get_open_invoices` | `ar_open` | customer debt (paid part taken off), receivables alert |
| `get_sales_by_date` (last 14 days) | `sales`, `sales_lines` | yesterday's sales, the Billz → SAP check |
| `get_stock_value` | `stock_value` | stock value |
| `get_supplier_balances` | `supplier_balances` | supplier debt for the 1C ↔ SAP comparison (the Director asks OPS Manager Bot; `scripts/ap_reconcile.py --sap-pushed`) |

## Setup

1. Open `push-ar-aging.ps1` in Notepad and fill in the four values at the
   top — the same four as before:
   - `$GatewayUrl` — `http://localhost:3000` (or `http://[::1]:3000`)
   - `$GatewayToken` — `API_TOKEN` from the gateway's `.env`
   - `$MgmgApiHost` — `mgmg-api-eeky.onrender.com`
   - `$PushSecret` — `SAP_PUSH_WEBHOOK_SECRET` from Render (`mgmg-shared`)
2. Check the setup (pushes nothing):
   ```powershell
   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1 -Check
   ```
   It says whether the Command Center and the gateway answer, and which of
   the three complete tools exist yet.
3. Run it once by hand:
   ```powershell
   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1
   ```
   It prints one line per tool and ends with `All done.` The same lines go
   to `push-ar-aging.log` next to the script.
4. Schedule it every 30 minutes: put `install-task.ps1` in the same folder
   (e.g. `C:\mgmg-push\`), open PowerShell with **Run as administrator**, and:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\mgmg-push\install-task.ps1
   ```
   It creates the task "MGMG SAP push": runs as SYSTEM (no window, works
   when nobody is logged in), on battery too, never two copies at once, and
   starts the first run right away. Running it again replaces the task; it
   warns if an older task also runs the push script. Check a run with
   `Get-ScheduledTaskInfo -TaskName "MGMG SAP push"` (`LastTaskResult` 0 =
   good, 1 = see `push-ar-aging.log`, 2 = settings not filled in).

Keep the machine (and `npm start`) running in the evening: the shop enters
the day's sales into SAP around 18:00–20:00, and the 08:00 Billz → SAP
check only sees what the last push carried.

## Troubleshooting

- **"Fill in … at the top of this file first"** — one of the four values is
  still `PASTE_…`.
- **Gateway not answering** — is `npm start` running? Is the token right?
- **"rejected: unauthorized"** — `$PushSecret` doesn't match Render's
  `SAP_PUSH_WEBHOOK_SECRET`.
- One tool failing doesn't stop the others; the run ends with "Done with N
  problem(s)" and exit code 1, which Task Scheduler shows as a failure.
- Works by hand but not from the task? The task runs as SYSTEM, which uses
  the machine's network settings — a proxy set only for your user isn't
  seen. Re-create the task under your own account in Task Scheduler.

## Security

- The gateway's token and HANA credentials never leave that machine; the
  script only forwards what the gateway's fixed tools return.
- The webhooks only accept the right secret (constant-time check) and can
  only write SAP snapshot rows (`ar_aging_snapshots`, `sap_gateway_snapshots`).
- The token sits in this file on that machine, like the gateway's own
  `.env`: keep the folder readable only by the account that runs the task.
