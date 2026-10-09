import { exec, normalizeLimit, tableRef } from './query.js';

export async function getInventory({ limit = 50, item_code, warehouse } = {}) {
  const safeLimit = normalizeLimit(limit, 50, 200);
  const item = String(item_code ?? '').trim().replace(/'/g, "''");
  const whs = String(warehouse ?? '').trim().replace(/'/g, "''");

  const itemFilter = item ? `AND W."ItemCode" = '${item}'` : '';
  const warehouseFilter = whs ? `AND W."WhsCode" = '${whs}'` : '';

  const rows = await exec(`
    SELECT
      W."ItemCode",
      I."ItemName",
      W."WhsCode",
      H."WhsName",
      W."OnHand",
      W."IsCommited",
      W."OnOrder"
    FROM ${tableRef('OITW')} W
    LEFT JOIN ${tableRef('OITM')} I
      ON I."ItemCode" = W."ItemCode"
    LEFT JOIN ${tableRef('OWHS')} H
      ON H."WhsCode" = W."WhsCode"
    WHERE 1 = 1
      ${itemFilter}
      ${warehouseFilter}
    ORDER BY W."ItemCode", W."WhsCode"
    LIMIT ${safeLimit}
  `);

  return { category: 'inventory', source: 'OITW', count: rows.length, data: rows };
}
