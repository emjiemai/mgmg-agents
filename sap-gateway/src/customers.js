import { exec, normalizeLimit, tableRef } from './query.js';

export async function getCustomers({ limit = 20, search = '' } = {}) {
  const safeLimit = normalizeLimit(limit, 20, 100);
  const table = tableRef('OCRD');
  const searchText = String(search ?? '').trim().replace(/'/g, "''");
  const filter = searchText
    ? `AND (UPPER("CardCode") LIKE UPPER('%${searchText}%') OR UPPER("CardName") LIKE UPPER('%${searchText}%'))`
    : '';

  const rows = await exec(`
    SELECT
      "CardCode",
      "CardName",
      "CardType",
      "Phone1",
      "E_Mail",
      "Balance",
      "Currency"
    FROM ${table}
    WHERE "CardType" = 'C'
      ${filter}
    ORDER BY "CardCode"
    LIMIT ${safeLimit}
  `);

  return { category: 'customers', source: 'OCRD', count: rows.length, data: rows };
}
