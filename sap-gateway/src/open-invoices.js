import { exec, normalizeLimit, tableRef } from './query.js';

/**
 * Complete list of currently open A/R invoices.
 *
 * "Open" means the invoice is not cancelled and still has an outstanding
 * balance. The balance is calculated from the SAP B1 header:
 *   DocTotal - PaidToDate
 *
 * Amounts: DocTotal / PaidToDate are SAP's local currency (USD here); the
 * invoices are written in so'm, so DocTotalFC / PaidFC carry the amounts as
 * written on the document (DocCur), and DocTotalSy / PaidSys the system
 * currency (so'm). SlpName is the sales person's name (OSLP).
 *
 * The PowerShell sync asks for up to 1000 rows and uses the number returned
 * to determine whether the result is complete.
 */
export async function getOpenInvoices({ limit = 1000 } = {}) {
  const safeLimit = normalizeLimit(limit, 1000, 5000);

  const rows = await exec(`
    SELECT
      H."DocEntry",
      H."DocNum",
      H."CardCode",
      H."CardName",
      H."DocDate",
      H."DocDueDate",
      H."DocTotal",
      H."PaidToDate",
      (H."DocTotal" - H."PaidToDate") AS "BalanceDue",
      H."DocCur",
      H."DocTotalFC",
      H."PaidFC",
      H."DocTotalSy",
      H."PaidSys",
      H."DocStatus",
      H."CANCELED",
      H."SlpCode",
      S."SlpName"
    FROM ${tableRef('OINV')} H
    LEFT JOIN ${tableRef('OSLP')} S
      ON S."SlpCode" = H."SlpCode"
    WHERE H."CANCELED" = 'N'
      AND (H."DocTotal" - H."PaidToDate") > 0
    ORDER BY H."DocDueDate" ASC, H."DocEntry" ASC
    LIMIT ${safeLimit}
  `);

  return {
    category: 'open_invoices',
    source: 'OINV/OSLP',
    count: rows.length,
    data: rows
  };
}
