import { exec, normalizeLimit, tableRef } from './query.js';

export async function getPayments({ limit = 20 } = {}) {
  const safeLimit = normalizeLimit(limit, 20, 100);
  const table = tableRef('ORCT');

  const rows = await exec(`
    SELECT
      "DocEntry",
      "DocNum",
      "CardCode",
      "CardName",
      "DocDate",
      "DocTotal",
      "DocCurr",
      "Canceled"
    FROM ${table}
    WHERE COALESCE("Canceled", 'N') = 'N'
    ORDER BY "DocDate" DESC, "DocEntry" DESC
    LIMIT ${safeLimit}
  `);

  return { category: 'payments', source: 'ORCT', count: rows.length, data: rows };
}
