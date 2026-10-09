import { exec, normalizeLimit, optionalDocNum, tableRef } from './query.js';

export async function getInvoices({ limit = 20, doc_num } = {}) {
  const safeLimit = normalizeLimit(limit, 20, 100);
  const safeDocNum = optionalDocNum(doc_num);
  const filter = safeDocNum === null ? '' : `AND "DocNum" = ${safeDocNum}`;

  const rows = await exec(`
    SELECT
      "DocEntry",
      "DocNum",
      "CardCode",
      "CardName",
      "DocDate",
      "DocDueDate",
      "DocTotal",
      "DocCur",
      "DocStatus",
      "CANCELED",
      "SlpCode"
    FROM ${tableRef('OINV')}
    WHERE "CANCELED" = 'N'
      ${filter}
    ORDER BY "DocDate" DESC, "DocEntry" DESC
    LIMIT ${safeLimit}
  `);

  return { category: 'invoices', source: 'OINV', count: rows.length, data: rows };
}
