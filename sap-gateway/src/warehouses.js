import { exec, normalizeLimit, tableRef } from './query.js';

export async function getWarehouses({ limit = 100 } = {}) {
  const safeLimit = normalizeLimit(limit, 100, 100);
  const table = tableRef('OWHS');

  const rows = await exec(`
    SELECT
      "WhsCode",
      "WhsName",
      "Inactive"
    FROM ${table}
    ORDER BY "WhsCode"
    LIMIT ${safeLimit}
  `);

  return { category: 'warehouses', source: 'OWHS', count: rows.length, data: rows };
}
