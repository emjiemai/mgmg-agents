import { exec, normalizeLimit, tableRef } from './query.js';

/**
 * Current stock value by item and warehouse.
 *
 * SAP B1 stores the warehouse quantity in OITW and the item's moving
 * average price in OITM.AvgPrice. StockValue is therefore:
 *   OnHand * AvgPrice
 *
 * Zero-stock rows are excluded because they do not contribute to inventory
 * value and would make the complete result unnecessarily large.
 */
export async function getStockValue({ limit = 10000 } = {}) {
  const safeLimit = normalizeLimit(limit, 10000, 50000);

  const rows = await exec(`
    SELECT
      W."ItemCode",
      I."ItemName",
      W."WhsCode",
      H."WhsName",
      W."OnHand",
      I."AvgPrice",
      (W."OnHand" * I."AvgPrice") AS "StockValue"
    FROM ${tableRef('OITW')} W
    INNER JOIN ${tableRef('OITM')} I
      ON I."ItemCode" = W."ItemCode"
    LEFT JOIN ${tableRef('OWHS')} H
      ON H."WhsCode" = W."WhsCode"
    WHERE W."OnHand" <> 0
    ORDER BY W."ItemCode", W."WhsCode"
    LIMIT ${safeLimit}
  `);

  return {
    category: 'stock_value',
    source: 'OITW/OITM',
    count: rows.length,
    data: rows
  };
}
