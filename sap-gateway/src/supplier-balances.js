import { exec, normalizeLimit, tableRef } from './query.js';

/**
 * Supplier debt: every supplier business partner (OCRD, CardType 'S') with a
 * balance, for the Director's 1C <-> SAP payables comparison.
 *
 * Balance is SAP's local currency (USD here), BalanceSys its system currency
 * (so'm, the figure compared with 1C), BalanceFC the partner's own currency.
 * SAP keeps money owed to a supplier as a negative (credit) balance.
 * LicTradNum is the tax id (ИНН/СТИР) used to pair suppliers with 1C.
 */
export async function getSupplierBalances({ limit = 5000 } = {}) {
  const safeLimit = normalizeLimit(limit, 5000, 20000);

  const rows = await exec(`
    SELECT
      C."CardCode",
      C."CardName",
      C."LicTradNum",
      C."CardType",
      C."Currency",
      C."Balance",
      C."BalanceSys",
      C."BalanceFC"
    FROM ${tableRef('OCRD')} C
    WHERE C."CardType" = 'S'
      AND (C."Balance" <> 0 OR C."BalanceSys" <> 0)
    ORDER BY C."BalanceSys" ASC, C."CardCode" ASC
    LIMIT ${safeLimit}
  `);

  return {
    category: 'supplier_balances',
    source: 'OCRD',
    count: rows.length,
    data: rows
  };
}
