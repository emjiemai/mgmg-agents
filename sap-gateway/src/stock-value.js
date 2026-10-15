import { exec, normalizeLimit, tableRef } from './query.js';

/**
 * Current stock value by item and warehouse.
 *
 * SAP B1 keeps each warehouse's own stock value in OITW.StockValue and its
 * own average price in OITW.AvgPrice. 15.10.2026: OnHand * OITM.AvgPrice
 * (the item-level price) came out 0 for all 18 warehouses — the item price
 * isn't kept there in this company — so the morning brief showed no stock.
 * StockValue is therefore, first that is non-zero:
 *   OITW.StockValue, OnHand * OITW.AvgPrice, OnHand * OITM.AvgPrice
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
      COALESCE(NULLIF(W."AvgPrice", 0), I."AvgPrice") AS "AvgPrice",
      COALESCE(NULLIF(W."StockValue", 0),
               NULLIF(W."OnHand" * W."AvgPrice", 0),
               W."OnHand" * I."AvgPrice") AS "StockValue"
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
