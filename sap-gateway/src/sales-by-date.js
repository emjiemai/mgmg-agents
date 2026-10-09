import { exec, tableRef } from './query.js';

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const MAX_DAYS = 92;

function requiredDate(value, name) {
  const date = String(value ?? '').trim();
  if (!DATE_RE.test(date) || Number.isNaN(Date.parse(`${date}T00:00:00Z`))) {
    throw new Error(`${name} must be a real date in YYYY-MM-DD format.`);
  }
  return date;
}

// The same columns from an A/R invoice (OINV/INV1, ObjType 13) or an A/R
// credit note (ORIN/RIN1, ObjType 14), so both halves of the UNION line up.
const branch = (objType, header, lines) => `
    SELECT
      ${objType} AS "ObjType",
      H."DocEntry",
      H."DocNum",
      H."CardCode",
      H."CardName",
      H."DocDate",
      H."DocDueDate",
      H."CreateDate",
      H."CreateTS",
      H."DocTotal",
      H."DocCur",
      H."DocTotalFC",
      H."DocTotalSy",
      H."DocStatus",
      H."CANCELED",
      H."SlpCode",
      H."UserSign",
      L."LineNum",
      L."ItemCode",
      L."Dscription",
      L."CodeBars",
      L."Quantity",
      L."Price",
      L."LineTotal",
      L."Currency" AS "LineCurrency",
      L."WhsCode"
    FROM ${tableRef(header)} H
    INNER JOIN ${tableRef(lines)} L
      ON L."DocEntry" = H."DocEntry"
    WHERE H."CANCELED" = 'N'
      AND (H."DocDate" BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD')
           OR H."CreateDate" BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD'))`;

/**
 * Invoice and credit-note lines dated OR entered in the requested range.
 *
 * "Entered" matters: a sale typed into SAP days later (or under an old
 * date) is still returned, so the Command Center's Billz -> SAP check can
 * see it and say it was late. Every row carries its document's header
 * fields and one line; ObjType tells an invoice (13) from a credit note (14).
 * DocTotalSy is the total in SAP's system currency (so'm), CreateTS the
 * entry time (hhmmss), UserSign the SAP user who entered it.
 */
export async function getSalesByDate({ from, to, limit = 10000 } = {}) {
  const fromDate = requiredDate(from, 'from');
  const toDate = requiredDate(to, 'to');

  if (fromDate > toDate) {
    throw new Error('from must not be later than to.');
  }
  const days = (Date.parse(`${toDate}T00:00:00Z`) - Date.parse(`${fromDate}T00:00:00Z`)) / 86_400_000;
  if (days > MAX_DAYS) {
    throw new Error(`The range may be at most ${MAX_DAYS} days.`);
  }

  const parsedLimit = Number.parseInt(limit ?? 10000, 10);
  if (!Number.isInteger(parsedLimit) || parsedLimit < 1 || parsedLimit > 50000) {
    throw new Error('limit must be an integer between 1 and 50000.');
  }

  const range = [fromDate, toDate, fromDate, toDate];
  const rows = await exec(`
    SELECT * FROM (
      ${branch(13, 'OINV', 'INV1')}
      UNION ALL
      ${branch(14, 'ORIN', 'RIN1')}
    ) T
    ORDER BY T."DocDate", T."ObjType", T."DocEntry", T."LineNum"
    LIMIT ${parsedLimit}
  `, [...range, ...range]);

  return {
    category: 'sales_by_date',
    source: 'OINV/INV1 + ORIN/RIN1',
    from: fromDate,
    to: toDate,
    count: rows.length,
    data: rows
  };
}
