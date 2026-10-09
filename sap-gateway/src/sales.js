import { exec, normalizeLimit, tableRef } from './query.js';

export async function getSales({ limit = 4 } = {}) {
  const safeLimit = normalizeLimit(limit, 4, 100);
  const table = tableRef('OINV');

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
    FROM ${table}
    WHERE "CANCELED" = 'N'
    ORDER BY "DocDate" DESC, "DocEntry" DESC
    LIMIT ${safeLimit}
  `);

  return { category: 'sales', source: 'OINV', count: rows.length, data: rows };
}
