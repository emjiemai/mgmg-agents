import { exec, normalizeLimit, tableRef } from './query.js';

export async function getProducts({ limit = 20, search = '' } = {}) {
  const safeLimit = normalizeLimit(limit, 20, 100);
  const table = tableRef('OITM');
  const searchText = String(search ?? '').trim();

  const params = searchText
    ? `AND (UPPER("ItemCode") LIKE UPPER('%${searchText.replace(/'/g, "''")}%') OR UPPER("ItemName") LIKE UPPER('%${searchText.replace(/'/g, "''")}%'))`
    : '';

  const rows = await exec(`
    SELECT
      "ItemCode",
      "ItemName",
      "ItmsGrpCod",
      "InvntItem",
      "SellItem",
      "PrchseItem"
    FROM ${table}
    WHERE 1 = 1
      ${params}
    ORDER BY "ItemCode"
    LIMIT ${safeLimit}
  `);

  return { category: 'products', source: 'OITM', count: rows.length, data: rows };
}
