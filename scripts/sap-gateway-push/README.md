# SAP gateway push script

Pushes a small AR-aging snapshot from the SAP gateway (the local Node.js
tool documented in `SAP_B1_AI_AGENT_TEACHING.md`, running on the server
laptop) into the Command Center's Postgres database. This machine reaches
*out* to the database on a schedule — nothing on the gateway's side changes,
and no inbound port is ever opened.

## One-time setup (do this once, from an admin machine — not the server laptop)

1. Run `create_role.sql` against the real database to create a scoped,
   least-privilege `sap_pusher` role (SELECT/INSERT/UPDATE on
   `ar_aging_snapshots` only — nothing else). Replace the placeholder
   password in that file with a real generated one before running it, and
   never commit the real password.
2. Build the connection string for the push script using that new role,
   not the admin one:
   ```
   postgresql://sap_pusher:<password>@<render-db-host>:5432/mgmg?sslmode=require
   ```
   Get `<render-db-host>` from Render's database dashboard (same host the
   admin `DATABASE_URL` uses, different user/password).

## Setup on the server laptop

1. Copy this whole `sap-gateway-push` folder onto the server laptop (same
   machine the gateway runs on).
2. Install dependencies:
   ```
   npm install
   ```
3. Test it once by hand first:
   ```
   set GATEWAY_URL=http://localhost:3000
   set GATEWAY_TOKEN=<the gateway's Bearer token>
   set DATABASE_URL=postgresql://sap_pusher:<password>@<render-db-host>:5432/mgmg?sslmode=require
   node push-ar-aging.js
   ```
   A successful run prints something like:
   ```
   [sap-gateway-push] fetching up to 100 invoices from http://localhost:3000 ...
   [sap-gateway-push] 23 open, non-cancelled invoice(s) to push
   [sap-gateway-push] done: 23 row(s) written for 2026-08-22, 0 skipped ...
   ```

## Schedule it (Windows Task Scheduler)

Once the manual run works, set it to run automatically:

1. Open Task Scheduler → Create Task
2. Trigger: e.g. every hour, or a few times a day — this doesn't need to be
   real-time, the Command Center already treats all its financial data as
   daily/periodic snapshots
3. Action: run `node.exe` with argument `push-ar-aging.js`, "Start in"
   pointed at this folder
4. Set the three environment variables above as **system or user
   environment variables** on that machine (not typed into the task
   itself, where they'd be visible in the task's properties) — Task
   Scheduler actions inherit the account's environment variables
   automatically

## Known gap

The gateway's `get_invoices` tool doesn't return `PaidToDate` yet, so this
script sets `balance_due` equal to `doc_total` for every invoice — correct
for a fully-unpaid open invoice, an overstatement for one that's been
partially paid. Ask whoever maintains the gateway to add `PaidToDate` to
`get_invoices`' response to fix this properly; it's a small addition on
that side, not something this script can work around.

## Security notes

- The `sap_pusher` database role can only touch `ar_aging_snapshots` —
  confirmed by the commented verification query at the bottom of
  `create_role.sql`.
- The gateway itself is untouched: still loopback-only, still requires its
  Bearer token, still only exposes its existing predefined read-only tools.
- If this credential or machine is ever compromised, the blast radius is
  "someone can write fake invoice rows into one snapshot table" — not
  access to any other data in the Command Center's database.
