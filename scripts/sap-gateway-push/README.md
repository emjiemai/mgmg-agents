# SAP push script

`push-ar-aging.ps1` sends SAP Business One data to the Command Center
(`mgmg-api`), which stores it for the brief, the receivables alert, the
Billz → SAP check and the Director's questions. It runs on the machine next
to SAP (the "server laptop"), on a schedule, and only makes **outbound**
connections. Plain Windows PowerShell 5.1 — nothing to install for the
script itself.

## Two modes

| Mode | Reads | Limits |
| ---- | ----- | ------ |
| **Database** (2026-10-03) | SAP's HANA database (schema `MGM`) with a read-only user, through the SAP HANA ODBC driver | every row; 16 kinds of data |
| **Gateway** (the old way) | the local SAP gateway's tools (`localhost:3000`) | at most 100 rows a kind (products 20) — receivables, stock and sales come out as "камида" |

The script uses the database when `$HanaServer`, `$HanaUser` and
`$HanaPassword` are filled in, and the gateway otherwise.

## What the database mode sends

| Name in the Command Center | SAP tables | What it is |
| -------------------------- | ---------- | ---------- |
| `sales_people` | OSLP | sales people |
| `ar_open` | OINV | open A/R invoices (customer debt) with what's paid so far |
| `sales` | OINV + ORIN | A/R invoices and credit notes, last 45 days |
| `sales_lines` | INV1 + RIN1 | their lines: item, quantity, warehouse |
| `inventory` | OITW + OITM | stock per item and warehouse (non-zero rows) |
| `products` | OITM + OITB | every item, with its group |
| `customers` | OCRD + OCRG | every business partner (no phones or e-mails) |
| `warehouses` | OWHS | warehouses |
| `payments` | ORCT | incoming payments, last 45 days |
| `payments_out` | OVPM | outgoing payments, last 45 days |
| `orders` | ORDR | open sales orders |
| `ap_open` | OPCH | open purchase invoices (what we owe suppliers) |
| `po_open` | OPOR + POR1 | open purchase order lines |
| `equipment` | OINS | customer equipment cards (serial numbers) |
| `service_calls` | OSCL | service calls |
| `service_contracts` | OCTR | service contracts |

Each kind is one POST to `/webhooks/sap-data/<name>/<secret>` and replaces
today's rows of that kind. Every column was checked against the SAP export
of 2026-10-02 — `sap_columns.json` keeps those tables' column names, and
`scripts/selfcheck.py` re-checks the SQL against it on every change.

## Setup (database mode)

1. **ODBC driver.** On the machine that runs the script: Windows → "ODBC
   Data Sources (64-bit)" → Drivers. `HDBODBC` must be there. It comes with
   the **SAP HANA client** (the SAP B1 client needs it too, so it's usually
   already installed). If it's missing, install SAP HANA Client 2.0 (64-bit)
   from SAP Development Tools.
2. **A read-only database user.** Whoever administers HANA (the same person
   who set up the gateway) creates it once, in HANA Studio or hdbsql:
   ```sql
   CREATE USER MGMG_READER PASSWORD "a-long-password" NO FORCE_FIRST_PASSWORD_CHANGE;
   GRANT SELECT ON SCHEMA "MGM" TO MGMG_READER;
   ```
   It can only read. Don't use SYSTEM or the SAP B1 admin user.
3. **Fill in the top of `push-ar-aging.ps1`:**
   - `$MgmgApiHost` — `mgmg-api-eeky.onrender.com`
   - `$PushSecret` — `SAP_PUSH_WEBHOOK_SECRET` from Render (`mgmg-shared`)
   - `$HanaServer` — host and SQL port, e.g. `192.168.1.10:30015` (the
     gateway's own `.env` has the same server)
   - `$HanaUser` / `$HanaPassword` — the read-only user from step 2
   - `$HanaSchema` — `MGM` (the company database)
4. **Run it once by hand:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1
   ```
   A good run prints one line per kind, ending with `All done.`:
   ```
   Reading SAP's database (192.168.1.10:30015, MGM) ...
     sales_people: 18 read, 18 stored.
     ar_open: 52 read, 52 stored.
     sales: 380 read, 380 stored.
     ...
   All done.
   ```
   The same lines go to `push-ar-aging.log` next to the script.
5. **Schedule it** (if it isn't already): Task Scheduler → Create Task →
   Trigger: Daily, repeat every **30 minutes**, indefinitely → Action:
   `powershell.exe` with `-ExecutionPolicy Bypass -File "C:\path\to\push-ar-aging.ps1"`
   → Settings: "If the task is already running" → **Do not start a new
   instance**. An existing task needs no change — the file name is the same.

The machine should stay on in the evening: the shop enters the day's sales
into SAP around 18:00–20:00, and the 08:00 Billz → SAP check can only see
what the last push carried (its message says so when the last push was
before midnight).

## Troubleshooting

- **"Could not connect to SAP's database"** — the driver name (`HDBODBC`;
  the 32-bit one is `HDBODBC32`), the server/port, or the user/password.
- **"SAP query failed: … invalid column name"** — the SAP version differs
  from the 2026-10-02 export; send the line from the log.
- **"rejected by the Command Center: unauthorized"** — `$PushSecret` doesn't
  match Render's `SAP_PUSH_WEBHOOK_SECRET`.
- One kind failing doesn't stop the others; the run ends with "Done with N
  problem(s)" and exit code 1, which Task Scheduler shows as a failure.

## Security

- The script reads with a user that can only `SELECT`, and sends only the
  columns listed in its SQL — no phones, e-mails or document comments.
- The webhook only accepts the right secret (constant-time check) and can
  only write SAP snapshot rows (`ar_aging_snapshots`, `sap_gateway_snapshots`).
- The password sits in this file on that machine, like the gateway's own
  `.env`: keep the folder readable only by the account that runs the task.
