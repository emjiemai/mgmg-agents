# SAP gateway push script

Pushes data from the SAP gateway (the local Node.js tool documented in
`SAP_B1_AI_AGENT_TEACHING.md`) to the Command Center's API, which does the
actual database write. This runs on the same machine as the gateway, and
needs **nothing installed** — it's plain PowerShell, using
`Invoke-RestMethod` the same way Abdulbosit's own test snippet already
does. No Node.js project, no npm install, no database driver.

Covers 7 of the gateway's 8 tools in one run: `get_invoices`, `get_orders`,
`get_products`, `get_customers`, `get_warehouses`, `get_inventory`,
`get_payments`. `get_sales` is skipped deliberately — it reads the exact
same source table as `get_invoices` (`OINV`) with a smaller interface, so
pulling both would just push the same data twice.

The gateway itself is completely untouched: still loopback-only, still
needs its Bearer token, still only exposes its existing predefined
read-only tools. This script only ever makes *outbound* connections — to
the gateway (local) and to the Command Center's API (a normal HTTPS call)
— so nothing on this machine needs to accept inbound traffic for this to
work.

## ⚠️ If you already had this script set up (config format changed)

The three config variables at the top changed shape to support pushing
multiple tools — `$PushUrl` (one full URL) became `$MgmgApiHost` +
`$PushSecret` (host and secret separately, so the script can build a
different URL per tool). **If you already filled in the old `$PushUrl`
version, you need to re-open the script and fill in the new variables** —
the old value won't carry over automatically. Your existing Task Scheduler
job doesn't need any changes; it already points at this same file and will
pick up the new behavior on its next run once the file itself is updated.

## Setup

1. Copy `push-ar-aging.ps1` onto the machine the gateway runs on (or run it
   straight from a synced/uploaded copy of this repo — either is fine, it's
   one self-contained file).
2. Open it in Notepad (or any editor) and fill in the four placeholders
   near the top:
   - `$GatewayToken` — the gateway's Bearer token (the same `API_TOKEN`
     value from its own `.env`)
   - `$MgmgApiHost` — the Command Center's API host, e.g.
     `mgmg-api-eeky.onrender.com` (no `https://` prefix, no trailing slash)
   - `$PushSecret` — `SAP_PUSH_WEBHOOK_SECRET` from Render's env group (not
     committed anywhere in this repo — get it from whoever manages the
     Render deployment)
3. Test it once by hand, from a PowerShell prompt on that machine:
   ```powershell
   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1
   ```
   A successful run prints one block per tool, ending with `All done.`:
   ```
   Fetching open invoices from http://localhost:3000 ...
   Got 23 invoice(s), pushing to Command Center ...
     invoices: 20 written, 3 skipped.
   Fetching from http://localhost:3000/tools/get_orders ...
     got 10 row(s), pushing to Command Center (orders) ...
     orders: 10 row(s) written.
   ...
   All done.
   ```
   If `http://localhost:3000` doesn't connect, try changing `$GatewayUrl`
   at the top of the script to `http://[::1]:3000` instead — some machines
   resolve `localhost` to an address the gateway isn't actually listening
   on.
4. **One tool failing doesn't stop the others** — each tool's fetch/push is
   wrapped independently, so if e.g. `get_warehouses` errors, you'll see a
   yellow warning for just that one and every other tool still runs and
   still pushes. Worth checking the output for any warnings even on an
   overall successful run.

## Schedule it (Windows Task Scheduler)

Already covered if you followed the earlier setup — the scheduled task
points at this same file, so it automatically picks up all 7 tools on its
next scheduled run. No changes needed in Task Scheduler itself.

If setting this up fresh:
1. Open Task Scheduler → Create Task
2. Trigger: **Daily**, repeat every **30 minutes**, indefinitely
3. Action:
   - Program: `powershell.exe`
   - Arguments: `-ExecutionPolicy Bypass -File "C:\path\to\push-ar-aging.ps1"`
4. Settings tab: "If the task is already running" → **Do not start a new
   instance**

## Known gaps

- **`get_invoices` doesn't return `PaidToDate`** yet, so the Command Center
  sets `balance_due` equal to `doc_total` for every invoice — correct for
  a fully-unpaid open invoice, an overstatement for one that's been
  partially paid.
- **The other 6 tools' exact response field names aren't confirmed** the
  way `get_sales`/`get_invoices`' were (verified against a real documented
  example response). The Command Center stores each tool's raw response in
  full regardless of field names, so nothing is lost — but the specific
  fields it tries to use as a natural key (`ItemCode`, `CardCode`,
  `WhsCode`, etc. — SAP Business One's standard names) might not match
  exactly what this particular gateway returns. If a tool's data looks
  wrong or duplicated once queried through OPS Manager Bot, that's the
  first thing to check — the raw JSON is always preserved regardless, so
  it's a quick fix, not a re-push.

Ask whoever maintains the gateway to add `PaidToDate` to `get_invoices`'
response to fix the first gap properly; it's a small addition on that
side, not something this script can work around.

## Security notes

- Both webhook endpoints (`/webhooks/sap-push/<secret>` and
  `/webhooks/sap-gateway-push/<tool>/<secret>`) only accept requests with
  the exact right secret in the URL, checked in constant time — same
  pattern every other webhook in this project uses. Both use the same
  `SAP_PUSH_WEBHOOK_SECRET`.
- These endpoints can only write to two tables (`ar_aging_snapshots` and
  `sap_gateway_snapshots`) — there is no path from this script to any
  other data in the Command Center.
- If this script or the token/secret inside it is ever exposed, the blast
  radius is "someone can push fake rows into those two tables" — not
  access to any other system.
