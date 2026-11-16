import { exec, normalizeLimit, tableRef } from './query.js';

/**
 * Stock movements for SAP history (F4, 2026-11-16): goods received from suppliers, transfers between
 * warehouses, goods receipts / issues (stock corrections), and open sales-order lines. The Command Center
 * keeps the document lines for two years and a daily stock picture, so «what came in, what moved, what
 * changed» can be answered for months — the base AIPRO (procurement) and OMAI (warehouse) need.
 *
 * Read-only SELECTs, values bound (`?`). These document tables share SAP's standard marketing-document
 * columns (the same as OPOR / POR1, checked by selfcheck through sap_columns.json "standard_copies").
 * No personal contact data.
 */

const result = (category, source, rows) => ({ category, source, count: rows.length, data: rows });

const days = (value, fallback, max) => {
  const parsed = Number.parseInt(value ?? fallback, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > max) {
    throw new Error(`days must be an integer between 1 and ${max}.`);
  }
  return parsed;
};

/** Goods receipt PO lines (OPDN + PDN1) of the last `days` days — what arrived from suppliers. */
export async function getGoodsReceipts({ days: wanted = 45, limit = 100000 } = {}) {
  const safeLimit = normalizeLimit(limit, 100000, 200000);
  const back = days(wanted, 45, 730);
  const rows = await exec(`
    SELECT H."DocEntry", H."DocNum", H."DocDate", H."CardCode", H."CardName", H."DocCur", H."CANCELED",
           H."CreateDate", L."LineNum", L."ItemCode", L."Dscription", L."Quantity", L."WhsCode", L."Price",
           L."Currency", L."LineTotal", L."BaseType", L."BaseEntry"
    FROM ${tableRef('OPDN')} H
    INNER JOIN ${tableRef('PDN1')} L ON L."DocEntry" = H."DocEntry"
    WHERE H."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
    ORDER BY H."DocDate" DESC, H."DocEntry" DESC, L."LineNum"
    LIMIT ${safeLimit}
  `, [-back]);
  return result('goods_receipts', 'OPDN/PDN1', rows);
}

/** Inventory transfer lines (OWTR + WTR1) of the last `days` days — from which warehouse to which. */
export async function getStockTransfers({ days: wanted = 45, limit = 100000 } = {}) {
  const safeLimit = normalizeLimit(limit, 100000, 200000);
  const back = days(wanted, 45, 730);
  const rows = await exec(`
    SELECT T."DocEntry", T."DocNum", T."DocDate", T."CANCELED", T."Filler", T."ToWhsCode", T."Comments",
           T."CreateDate", W."LineNum", W."ItemCode", W."Dscription", W."Quantity", W."FromWhsCod", W."WhsCode"
    FROM ${tableRef('OWTR')} T
    INNER JOIN ${tableRef('WTR1')} W ON W."DocEntry" = T."DocEntry"
    WHERE T."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
    ORDER BY T."DocDate" DESC, T."DocEntry" DESC, W."LineNum"
    LIMIT ${safeLimit}
  `, [-back]);
  return result('stock_transfers', 'OWTR/WTR1', rows);
}

/** Goods receipts (OIGN, ObjType 59) and goods issues (OIGE, 60) of the last `days` days — stock put in or
 *  taken out without a purchase or a sale (corrections, write-offs, own use). */
export async function getInventoryMoves({ days: wanted = 45, limit = 100000 } = {}) {
  const safeLimit = normalizeLimit(limit, 100000, 200000);
  const back = days(wanted, 45, 730);
  const rows = await exec(`
    SELECT X."ObjType", X."DocEntry", X."DocNum", X."DocDate", X."CANCELED", X."Comments", X."LineNum",
           X."ItemCode", X."Dscription", X."Quantity", X."WhsCode", X."LineTotal"
    FROM (
      SELECT 59 AS "ObjType", R."DocEntry", R."DocNum", R."DocDate", R."CANCELED", R."Comments", N."LineNum",
             N."ItemCode", N."Dscription", N."Quantity", N."WhsCode", N."LineTotal"
      FROM ${tableRef('OIGN')} R INNER JOIN ${tableRef('IGN1')} N ON N."DocEntry" = R."DocEntry"
      WHERE R."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
      UNION ALL
      SELECT 60, G."DocEntry", G."DocNum", G."DocDate", G."CANCELED", G."Comments", E."LineNum",
             E."ItemCode", E."Dscription", E."Quantity", E."WhsCode", E."LineTotal"
      FROM ${tableRef('OIGE')} G INNER JOIN ${tableRef('IGE1')} E ON E."DocEntry" = G."DocEntry"
      WHERE G."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
    ) X
    ORDER BY X."DocDate" DESC, X."DocEntry" DESC, X."LineNum"
    LIMIT ${safeLimit}
  `, [-back, -back]);
  return result('inventory_moves', 'OIGN/IGN1-OIGE/IGE1', rows);
}

/** Every open sales-order line (ORDR + RDR1) — what customers ordered and is still to deliver. */
export async function getOpenSalesOrderLines({ limit = 50000 } = {}) {
  const safeLimit = normalizeLimit(limit, 50000, 100000);
  const rows = await exec(`
    SELECT O."DocEntry", O."DocNum", O."DocDate", O."DocDueDate", O."CardCode", O."CardName", O."SlpCode",
           O."DocCur", D."LineNum", D."ItemCode", D."Dscription", D."Quantity", D."OpenQty", D."WhsCode",
           D."Price", D."Currency", D."LineTotal", D."ShipDate"
    FROM ${tableRef('ORDR')} O
    INNER JOIN ${tableRef('RDR1')} D ON D."DocEntry" = O."DocEntry"
    WHERE O."DocStatus" = 'O' AND O."CANCELED" = 'N' AND D."LineStatus" = 'O'
    ORDER BY O."DocDueDate", O."DocEntry", D."LineNum"
    LIMIT ${safeLimit}
  `);
  return result('so_open_lines', 'ORDR/RDR1', rows);
}
