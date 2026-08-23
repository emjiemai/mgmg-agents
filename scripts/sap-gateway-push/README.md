# SAP gateway push script

Pushes a small AR-aging snapshot from the SAP gateway (the local Node.js
tool documented in `SAP_B1_AI_AGENT_TEACHING.md`) to the Command Center's
API, which does the actual database write. This runs on the same machine
as the gateway, and needs **nothing installed** — it's plain PowerShell,
using `Invoke-RestMethod` the same way Abdulbosit's own test snippet
already does. No Node.js project, no npm install, no database driver.

The gateway itself is completely untouched: still loopback-only, still
needs its Bearer token, still only exposes its existing predefined
read-only tools. This script only ever makes *outbound* connections — to
the gateway (local) and to the Command Center's API (a normal HTTPS call)
— so nothing on this machine needs to accept inbound traffic for this to
work.

## Setup

1. Copy `push-ar-aging.ps1` onto the machine the gateway runs on (or run it
   straight from a synced/uploaded copy of this repo — either is fine, it's
   one self-contained file).
2. Open it in Notepad (or any editor) and fill in the three placeholders
   near the top:
   - `$GatewayToken` — the gateway's Bearer token (the same `API_TOKEN`
     value from its own `.env`)
   - `$PushUrl` — `https://<mgmg-api-host>/webhooks/sap-push/<secret>`
     (get both the host and the secret from whoever manages the Command
     Center's Render deployment — the secret is `SAP_PUSH_WEBHOOK_SECRET`
     in Render's env group, not committed anywhere in this repo)
3. Test it once by hand, from a PowerShell prompt on that machine:
   ```powershell
   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1
   ```
   A successful run prints something like:
   ```
   Fetching open invoices from http://localhost:3000 ...
   Got 23 invoice(s) from the gateway, pushing to Command Center ...
   Done: 23 row(s) written, 0 skipped.
   ```
   If `http://localhost:3000` doesn't connect, try changing `$GatewayUrl`
   at the top of the script to `http://[::1]:3000` instead — some machines
   resolve `localhost` to an address the gateway isn't actually listening
   on.

## Schedule it (Windows Task Scheduler)

Once the manual run works:

1. Open Task Scheduler → Create Task
2. Trigger: e.g. every hour, or a few times a day — this doesn't need to be
   real-time, the Command Center already treats all its financial data as
   daily/periodic snapshots, same as this
3. Action:
   - Program: `powershell.exe`
   - Arguments: `-ExecutionPolicy Bypass -File "C:\path\to\push-ar-aging.ps1"`
4. Nothing else to configure — the credentials are already filled into the
   script file itself in step 2 above, no environment variables needed

## Known gap

The gateway's `get_invoices` tool doesn't return `PaidToDate` yet, so the
Command Center sets `balance_due` equal to `doc_total` for every invoice —
correct for a fully-unpaid open invoice, an overstatement for one that's
been partially paid. Ask whoever maintains the gateway to add `PaidToDate`
to `get_invoices`' response to fix this properly; it's a small addition on
that side, not something this script can work around.

## Security notes

- The Command Center's `/webhooks/sap-push/<secret>` endpoint only accepts
  requests with the exact right secret in the URL, checked in constant
  time — same pattern every other webhook in this project uses.
- That endpoint can only write to one table (`ar_aging_snapshots`) — there
  is no path from this script to any other data in the Command Center.
- If this script or the token/secret inside it is ever exposed, the blast
  radius is "someone can push fake invoice rows into one snapshot table" —
  not access to any other system.
