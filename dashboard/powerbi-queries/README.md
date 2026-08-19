# Power BI Dashboard v1 — build instructions

The dashboard reads **only** from PostgreSQL. It never touches SAP or amoCRM
directly: the agents write daily snapshots, Power BI reads them. That keeps the
report fast, keeps SAP credentials out of Power BI, and means a report refresh
can never put load on the ERP.

## 1. Connect

PostgreSQL is bound to `127.0.0.1` on the VPS, so connect over an SSH tunnel
rather than opening 5432 to the internet:

```bash
ssh -N -L 5432:localhost:5432 user@your-vps-ip
```

Then in Power BI Desktop: **Get Data → PostgreSQL database**

| Field    | Value                    |
| -------- | ------------------------ |
| Server   | `localhost:5432`         |
| Database | `mgmg`                   |
| Mode     | **Import** (not DirectQuery) |

Use a dedicated read-only Postgres role for the report:

```sql
CREATE ROLE powerbi LOGIN PASSWORD 'set-a-strong-password';
GRANT CONNECT ON DATABASE mgmg TO powerbi;
GRANT USAGE ON SCHEMA public TO powerbi;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO powerbi;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO powerbi;
```

## 2. Load the queries

Create one Power Query per block in the SQL files, named exactly as the model
expects:

| Query name     | Source file                  | Block             |
| -------------- | ---------------------------- | ----------------- |
| `Cash`         | `01_cash_and_ar.sql`         | QUERY: Cash       |
| `AR Aging`     | `01_cash_and_ar.sql`         | QUERY: AR Aging   |
| `Sales`        | `01_cash_and_ar.sql`         | QUERY: Sales      |
| `Pipeline`     | `02_pipeline_and_ops.sql`    | QUERY: Pipeline   |
| `Attendance`   | `02_pipeline_and_ops.sql`    | QUERY: Attendance |
| `Overdue Tasks`| `02_pipeline_and_ops.sql`    | QUERY: Overdue Tasks |
| `Daily Briefs` | `02_pipeline_and_ops.sql`    | QUERY: Daily Briefs |
| `Agent Health` | `02_pipeline_and_ops.sql`    | QUERY: Agent Health |
| `Alerts`       | `02_pipeline_and_ops.sql`    | QUERY: Alerts     |
| `Date`         | `02_pipeline_and_ops.sql`    | QUERY: Date       |

## 3. Model

1. **File → Options → Data Load → uncheck "Auto date/time"**. The shared `Date`
   table replaces it; leaving it on creates a hidden date table per column and
   breaks cross-source filtering.
2. Mark `Date` as a date table on `Date[Date]`.
3. Relate `Date[Date]` **1 → \*** to `[Date]` on: `Cash`, `AR Aging`, `Sales`,
   `Pipeline`, `Attendance`, `Overdue Tasks`, `Daily Briefs`, `Agent Health`,
   `Alerts`. All single-direction.
4. Sort `AR Aging[Bucket]` by `AR Aging[Bucket Sort]`.
5. Create a blank table called `Measures` and paste in `measures.dax`.

### The one modelling trap

Every fact table is a **daily snapshot**. `SUM(Balance Due UZS)` across a month
adds the same invoice ~30 times. Every measure in `measures.dax` therefore
pins to the latest snapshot date in the current filter context. If you add a
measure, follow that pattern — do not sum a snapshot column directly.

## 4. Page layout (v1)

**Page 1 — CEO Overview**

- Row of cards: `Cash Balance`, `Overdue AR`, `Pipeline Value`, `Stalled Deals`,
  `Overdue Tasks`
- `Data Freshness` card, top right — small, always visible
- Line chart: `Cash Balance` and `Overdue AR` by date, last 90 days
- Stacked column: `Overdue AR` by `Bucket`, coloured by `Overdue AR Color`
- Bar: `Overdue AR` by `Owner`, top 10
- Bar: `Pipeline Value` by `Stage`
- Division slicer (`AR Aging[Division]`), synced across pages

**Page 2 — Receivables detail**

- Table: Customer, Invoice No, Due Date, Days Overdue, Balance Due UZS, Owner
- Filter: `Is Overdue = True`, sorted by Balance Due descending
- Cards: `Overdue AR 90+`, `DSO Weighted Days`, `Overdue Invoice Count`

**Page 3 — Pipeline**

- Matrix: Pipeline × Stage, values `Pipeline Value` and `Deals`
- Cards: `New Leads 24h`, `Average Deal Size`, `Stalled Deals %`

**Page 4 — Agent Health** (hidden from the CEO, for the automation team)

- `Agent Failures 7D` card, table of `Agent Health` by agent and system,
  `Daily Briefs` table showing `Failed Sources` per day

## 5. Refresh

Snapshots are written by the morning agents (08:00 and 09:00 Tashkent). Schedule
the dataset refresh for **10:00 Tashkent (05:00 UTC)** so it always reads a
complete day. A gateway is required for the Power BI Service to reach the VPS.
