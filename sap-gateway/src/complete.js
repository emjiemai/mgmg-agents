import { exec, normalizeLimit, tableRef } from './query.js';

/**
 * The rest of what the Command Center needs from SAP, complete (2026-10-16):
 * every row, no 100-row cap, so the push never has to change again for the
 * owner's plan (docs/sap-gateway-tools.md, "Everything the plan needs").
 *
 * Read-only SELECTs, values bound (`?`), columns checked against SAP's own
 * column export by selfcheck. No personal contact data (phones, e-mails,
 * addresses) — it isn't needed and the rows go on to the AI.
 */

const result = (category, source, rows) => ({ category, source, count: rows.length, data: rows });

const days = (value, fallback, max) => {
  const parsed = Number.parseInt(value ?? fallback, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > max) {
    throw new Error(`days must be an integer between 1 and ${max}.`);
  }
  return parsed;
};

/** Every customer (OCRD, CardType C) with its balance, group and sales person. */
export async function getCustomersAll({ limit = 20000 } = {}) {
  const safeLimit = normalizeLimit(limit, 20000, 50000);
  const rows = await exec(`
    SELECT C."CardCode", C."CardName", C."LicTradNum", C."GroupCode", G."GroupName", C."Currency",
           C."Balance", C."BalanceSys", C."CreditLine", C."SlpCode", C."City", C."validFor", C."frozenFor",
           C."CreateDate", C."UpdateDate"
    FROM ${tableRef('OCRD')} C
    LEFT JOIN ${tableRef('OCRG')} G ON G."GroupCode" = C."GroupCode"
    WHERE C."CardType" = 'C'
    ORDER BY C."CardCode"
    LIMIT ${safeLimit}
  `);
  return result('customers', 'OCRD/OCRG', rows);
}

/** Every item (OITM) with its group, stock, reorder levels and last purchase. */
export async function getItemsAll({ limit = 50000 } = {}) {
  const safeLimit = normalizeLimit(limit, 50000, 100000);
  const rows = await exec(`
    SELECT I."ItemCode", I."ItemName", I."ItmsGrpCod", B."ItmsGrpNam", I."CodeBars", I."InvntryUom",
           I."OnHand", I."IsCommited", I."OnOrder", I."MinLevel", I."MaxLevel", I."AvgPrice",
           I."LastPurPrc", I."LastPurDat", I."InvntItem", I."SellItem", I."PrchseItem", I."validFor", I."frozenFor"
    FROM ${tableRef('OITM')} I
    LEFT JOIN ${tableRef('OITB')} B ON B."ItmsGrpCod" = I."ItmsGrpCod"
    ORDER BY I."ItemCode"
    LIMIT ${safeLimit}
  `);
  return result('products', 'OITM/OITB', rows);
}

/** Every open sales order (ORDR). */
export async function getOpenOrders({ limit = 20000 } = {}) {
  const safeLimit = normalizeLimit(limit, 20000, 50000);
  const rows = await exec(`
    SELECT O."DocEntry", O."DocNum", O."CardCode", O."CardName", O."DocDate", O."DocDueDate", O."DocStatus",
           O."CANCELED", O."DocCur", O."DocTotal", O."DocTotalSy", O."DocTotalFC", O."SlpCode", O."Comments"
    FROM ${tableRef('ORDR')} O
    WHERE O."DocStatus" = 'O' AND O."CANCELED" = 'N'
    ORDER BY O."DocDate" DESC, O."DocEntry" DESC
    LIMIT ${safeLimit}
  `);
  return result('orders', 'ORDR', rows);
}

async function payments(table, category, { days: wanted = 45, limit = 20000 } = {}) {
  const safeLimit = normalizeLimit(limit, 20000, 50000);
  const back = days(wanted, 45, 400);
  const rows = await exec(`
    SELECT P."DocEntry", P."DocNum", P."DocDate", P."CardCode", P."CardName", P."DocCurr", P."DocTotal",
           P."DocTotalSy", P."DocTotalFC", P."CashSum", P."TrsfrSum", P."CheckSum", P."CreditSum", P."Canceled",
           P."Comments"
    FROM ${tableRef(table)} P
    WHERE P."Canceled" = 'N' AND P."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
    ORDER BY P."DocDate" DESC, P."DocEntry" DESC
    LIMIT ${safeLimit}
  `, [-back]);
  return result(category, table, rows);
}

/** Incoming payments from customers (ORCT) of the last `days` days. */
export const getIncomingPayments = (input = {}) => payments('ORCT', 'payments', input);

/** Outgoing payments to suppliers (OVPM) of the last `days` days. */
export const getOutgoingPayments = (input = {}) => payments('OVPM', 'payments_out', input);

/** Every open supplier invoice (OPCH) with what is still to pay — for the cash calendar. */
export async function getOpenPurchaseInvoices({ limit = 20000 } = {}) {
  const safeLimit = normalizeLimit(limit, 20000, 50000);
  const rows = await exec(`
    SELECT H."DocEntry", H."DocNum", H."NumAtCard", H."CardCode", H."CardName", H."LicTradNum", H."DocDate",
           H."DocDueDate", H."DocStatus", H."CANCELED", H."DocCur", H."DocTotal", H."PaidToDate",
           H."DocTotalSy", H."PaidSys", H."DocTotalFC", H."PaidFC"
    FROM ${tableRef('OPCH')} H
    WHERE H."DocStatus" = 'O' AND H."CANCELED" = 'N'
    ORDER BY H."DocDueDate", H."DocEntry"
    LIMIT ${safeLimit}
  `);
  return result('ap_open', 'OPCH', rows);
}

/** Every open purchase order line (OPOR + POR1) — what is ordered and still to arrive. */
export async function getOpenPurchaseOrders({ limit = 50000 } = {}) {
  const safeLimit = normalizeLimit(limit, 50000, 100000);
  const rows = await exec(`
    SELECT H."DocEntry", H."DocNum", H."CardCode", H."CardName", H."DocDate", H."DocDueDate", H."DocCur",
           L."LineNum", L."ItemCode", L."Dscription", L."Quantity", L."OpenQty", L."Price", L."Currency",
           L."LineTotal", L."ShipDate", L."WhsCode", L."LineStatus"
    FROM ${tableRef('OPOR')} H
    INNER JOIN ${tableRef('POR1')} L ON L."DocEntry" = H."DocEntry"
    WHERE H."DocStatus" = 'O' AND H."CANCELED" = 'N' AND L."LineStatus" = 'O'
    ORDER BY H."DocDueDate", H."DocEntry", L."LineNum"
    LIMIT ${safeLimit}
  `);
  return result('po_open', 'OPOR/POR1', rows);
}

/** Every sales employee (OSLP) — names for SlpCode. */
export async function getSalesPeople({ limit = 1000 } = {}) {
  const safeLimit = normalizeLimit(limit, 1000, 5000);
  const rows = await exec(`
    SELECT S."SlpCode", S."SlpName", S."GroupCode", S."Active", S."Memo"
    FROM ${tableRef('OSLP')} S
    ORDER BY S."SlpCode"
    LIMIT ${safeLimit}
  `);
  return result('sales_people', 'OSLP', rows);
}

/** Every customer equipment card (OINS) — for service reminders. */
export async function getEquipment({ limit = 50000 } = {}) {
  const safeLimit = normalizeLimit(limit, 50000, 100000);
  const rows = await exec(`
    SELECT E."insID", E."customer", E."custmrName", E."itemCode", E."itemName", E."manufSN", E."internalSN",
           E."status", E."createDate"
    FROM ${tableRef('OINS')} E
    ORDER BY E."insID"
    LIMIT ${safeLimit}
  `);
  return result('equipment', 'OINS', rows);
}

/** Service calls (OSCL): every one still open, and every one created in the last `days` days —
 *  with the technician's (OHEM) and the assignee's (OUSR) names, not only their ids (2026-10-21). */
export async function getServiceCalls({ days: wanted = 180, limit = 50000 } = {}) {
  const safeLimit = normalizeLimit(limit, 50000, 100000);
  const back = days(wanted, 180, 730);
  const rows = await exec(`
    SELECT Q."callID", Q."subject", Q."customer", Q."custmrName", Q."itemCode", Q."itemName", Q."manufSN",
           Q."internalSN", Q."status", Q."priority", Q."origin", Q."problemTyp", Q."callType", Q."createDate",
           Q."closeDate", Q."assignee", Q."technician",
           TRIM(COALESCE(TCH."lastName", '') || ' ' || COALESCE(TCH."firstName", '')) AS "TechnicianName",
           USR."U_NAME" AS "AssigneeName"
    FROM ${tableRef('OSCL')} Q
    LEFT JOIN ${tableRef('OHEM')} TCH ON TCH."empID" = Q."technician"
    LEFT JOIN ${tableRef('OUSR')} USR ON USR."USERID" = Q."assignee"
    WHERE Q."closeDate" IS NULL OR Q."createDate" >= ADD_DAYS(CURRENT_DATE, ?)
    ORDER BY Q."createDate" DESC, Q."callID" DESC
    LIMIT ${safeLimit}
  `, [-back]);
  return result('service_calls', 'OSCL', rows);
}

/** Every service contract (OCTR) with its dates — renewals. */
export async function getServiceContracts({ limit = 20000 } = {}) {
  const safeLimit = normalizeLimit(limit, 20000, 50000);
  const rows = await exec(`
    SELECT T."ContractID", T."CstmrCode", T."CstmrName", T."CntrcType", T."Status", T."StartDate", T."EndDate",
           T."TermDate", T."Descriptio"
    FROM ${tableRef('OCTR')} T
    ORDER BY T."EndDate", T."ContractID"
    LIMIT ${safeLimit}
  `);
  return result('service_contracts', 'OCTR', rows);
}

/**
 * Quantity sold per item and warehouse in the last `days` days: invoice lines
 * minus credit-note lines (for reordering: «what sells, what doesn't»).
 */
export async function getItemSales({ days: wanted = 90, limit = 100000 } = {}) {
  const safeLimit = normalizeLimit(limit, 100000, 200000);
  const back = days(wanted, 90, 400);
  const rows = await exec(`
    SELECT X."ItemCode", X."WhsCode", SUM(X."Qty") AS "Quantity", SUM(X."Amount") AS "LineTotal",
           MAX(X."DocDate") AS "LastSold", ${back} AS "Days"
    FROM (
      SELECT L."ItemCode", L."WhsCode", L."Quantity" AS "Qty", L."LineTotal" AS "Amount", H."DocDate"
      FROM ${tableRef('OINV')} H INNER JOIN ${tableRef('INV1')} L ON L."DocEntry" = H."DocEntry"
      WHERE H."CANCELED" = 'N' AND H."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
      UNION ALL
      SELECT L."ItemCode", L."WhsCode", -L."Quantity", -L."LineTotal", H."DocDate"
      FROM ${tableRef('ORIN')} H INNER JOIN ${tableRef('RIN1')} L ON L."DocEntry" = H."DocEntry"
      WHERE H."CANCELED" = 'N' AND H."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
    ) X
    WHERE X."ItemCode" IS NOT NULL
    GROUP BY X."ItemCode", X."WhsCode"
    ORDER BY X."ItemCode", X."WhsCode"
    LIMIT ${safeLimit}
  `, [-back, -back]);
  return result('item_sales_90d', 'OINV/INV1-ORIN/RIN1', rows);
}
