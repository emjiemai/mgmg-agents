/**
 * Pushes a small AR-aging snapshot from the local SAP gateway into the
 * Command Center's Postgres database.
 *
 * Why this exists: the gateway is deliberately bound to localhost only (see
 * the teaching docs it ships with) and stays that way -- nothing about this
 * script asks for that to change. Instead of the cloud app reaching in here
 * (which would need an open inbound port), this machine reaches OUT to the
 * cloud database on a schedule, which is a normal outbound connection no
 * firewall usually blocks. Same "snapshot now, read later" pattern every
 * other agent in this project already uses.
 *
 * Data minimization, matching the gateway's own teaching docs: this pulls
 * at most GATEWAY_FETCH_LIMIT open invoices (default 100, the gateway's own
 * max), not the whole database, and writes only the columns the destination
 * table needs.
 *
 * Known gap: the gateway's get_invoices tool does not return PaidToDate, so
 * balance_due is set equal to doc_total for every invoice -- correct for a
 * fully-unpaid open invoice, an overstatement for a partially-paid one.
 * Ask whoever maintains the gateway to add PaidToDate to get_invoices'
 * response to fix this properly; nothing here silently hides the gap, it's
 * flagged in the row comment below and in the run summary this prints.
 *
 * Usage:
 *   set GATEWAY_URL=http://localhost:3000
 *   set GATEWAY_TOKEN=...
 *   set DATABASE_URL=postgresql://sap_pusher:...@<host>:5432/mgmg?sslmode=require
 *   node push-ar-aging.js
 *
 * Meant to be run on a schedule (Windows Task Scheduler) on the same
 * machine the gateway runs on -- see README.md in this folder.
 */

const { Client } = require("pg");

const GATEWAY_URL = process.env.GATEWAY_URL || "http://localhost:3000";
const GATEWAY_TOKEN = process.env.GATEWAY_TOKEN;
const DATABASE_URL = process.env.DATABASE_URL;
const FETCH_LIMIT = Number(process.env.GATEWAY_FETCH_LIMIT || 100);

function fail(message) {
  console.error(`[sap-gateway-push] FATAL: ${message}`);
  process.exit(1);
}

if (!GATEWAY_TOKEN) fail("GATEWAY_TOKEN is not set");
if (!DATABASE_URL) fail("DATABASE_URL is not set");

/** One of 'current' | '1_30' | '31_60' | '61_90' | '90_plus' -- mirrors
 * aging_bucket() in integrations/sap/client.py exactly, so a snapshot
 * pushed from here and one pulled the "proper" way (if that ever becomes
 * reachable too) bucket identically. */
function agingBucket(daysOverdue) {
  if (daysOverdue <= 0) return "current";
  if (daysOverdue <= 30) return "1_30";
  if (daysOverdue <= 60) return "31_60";
  if (daysOverdue <= 90) return "61_90";
  return "90_plus";
}

function daysBetween(dueDateStr, asOf) {
  if (!dueDateStr) return 0;
  const due = new Date(dueDateStr);
  if (Number.isNaN(due.getTime())) return 0;
  const diffMs = asOf.getTime() - due.getTime();
  return Math.max(Math.round(diffMs / (1000 * 60 * 60 * 24)), 0);
}

/** UZS -> tiyin (1 UZS = 100 tiyin), matching integrations/common/money.py's
 * to_tiyin() convention used everywhere else in this project. */
function toTiyin(amount) {
  if (amount === null || amount === undefined) return 0;
  return Math.round(Number(amount) * 100);
}

async function fetchOpenInvoices() {
  const response = await fetch(`${GATEWAY_URL}/tools/get_invoices`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${GATEWAY_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ limit: FETCH_LIMIT }),
  });

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`gateway returned HTTP ${response.status}: ${text.slice(0, 300)}`);
  }

  const body = await response.json();
  if (!body.ok) {
    throw new Error(`gateway reported an error: ${body.error || "unknown"}`);
  }

  const rows = body.data || [];
  // Open, not cancelled -- same filter integrations/sap/client.py applies
  // against the real Service Layer (field names differ; this gateway's own
  // shape uses DocStatus/'O' and CANCELED/'N', per its teaching doc).
  return rows.filter((row) => row.DocStatus === "O" && row.CANCELED === "N");
}

async function main() {
  console.log(`[sap-gateway-push] fetching up to ${FETCH_LIMIT} invoices from ${GATEWAY_URL} ...`);
  const invoices = await fetchOpenInvoices();
  console.log(`[sap-gateway-push] ${invoices.length} open, non-cancelled invoice(s) to push`);

  const asOf = new Date();
  const snapshotDate = asOf.toISOString().slice(0, 10); // YYYY-MM-DD

  const client = new Client({ connectionString: DATABASE_URL });
  await client.connect();

  let written = 0;
  let skipped = 0;
  try {
    await client.query("BEGIN");
    for (const inv of invoices) {
      if (!inv.DocEntry || !inv.CardCode) {
        skipped += 1;
        continue; // both are NOT NULL in the destination table -- never insert a row missing either
      }
      const daysOverdue = daysBetween(inv.DocDueDate, asOf);
      const docTotalTiyin = toTiyin(inv.DocTotal);
      // See the file-level comment: PaidToDate isn't in this gateway's
      // response yet, so balance_due == doc_total for now.
      const paidToDateTiyin = 0;
      const balanceDueTiyin = docTotalTiyin - paidToDateTiyin;

      await client.query(
        `INSERT INTO ar_aging_snapshots
           (snapshot_date, division, doc_entry, doc_num, card_code, card_name,
            doc_date, due_date, days_overdue, aging_bucket, currency,
            doc_total_tiyin, paid_to_date_tiyin, balance_due_tiyin,
            sales_person_code, sales_person_name)
         VALUES ($1, NULL, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, NULL)
         ON CONFLICT (snapshot_date, doc_entry) DO UPDATE SET
           doc_num = EXCLUDED.doc_num,
           card_code = EXCLUDED.card_code,
           card_name = EXCLUDED.card_name,
           doc_date = EXCLUDED.doc_date,
           due_date = EXCLUDED.due_date,
           days_overdue = EXCLUDED.days_overdue,
           aging_bucket = EXCLUDED.aging_bucket,
           currency = EXCLUDED.currency,
           doc_total_tiyin = EXCLUDED.doc_total_tiyin,
           paid_to_date_tiyin = EXCLUDED.paid_to_date_tiyin,
           balance_due_tiyin = EXCLUDED.balance_due_tiyin,
           sales_person_code = EXCLUDED.sales_person_code`,
        [
          snapshotDate,
          inv.DocEntry,
          inv.DocNum || null,
          inv.CardCode,
          inv.CardName || null,
          inv.DocDate || null,
          inv.DocDueDate || null,
          daysOverdue,
          agingBucket(daysOverdue),
          inv.DocCur || "UZS",
          docTotalTiyin,
          paidToDateTiyin,
          balanceDueTiyin,
          inv.SlpCode && inv.SlpCode !== -1 ? inv.SlpCode : null,
        ]
      );
      written += 1;
    }
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    await client.end();
  }

  console.log(
    `[sap-gateway-push] done: ${written} row(s) written for ${snapshotDate}, ${skipped} skipped (missing required field). ` +
      `Note: balance_due currently equals doc_total for every row (PaidToDate not available from the gateway yet).`
  );
}

main().catch((err) => {
  console.error(`[sap-gateway-push] FAILED: ${err.message}`);
  process.exit(1);
});
