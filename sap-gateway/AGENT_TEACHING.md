# Agent teaching notes

## Gateway role

This service is the only component that talks to SAP Business One HANA.
Clients call HTTP tools; clients do not send SQL and do not need HANA
credentials.

## Available complete tools

- `get_open_invoices` — all currently open A/R invoices, with `BalanceDue`,
  the so'm amounts (`DocTotalFC`, `PaidFC`, `DocTotalSy`, `PaidSys`) and
  `SlpName`.
- `get_sales_by_date` — A/R invoice (`ObjType` 13) and credit-note (14) lines
  dated **or entered** in an inclusive `from` / `to` range (max 92 days),
  with `CreateDate`, `CreateTS`, `UserSign`, `DocTotalSy`, `CodeBars`.
- `get_supplier_balances` — every supplier (OCRD `CardType` 'S') with a
  balance: `Balance`, `BalanceSys`, `BalanceFC`, `LicTradNum`.
- `get_stock_value` — current stock by item and warehouse, with calculated
  `StockValue`.

## Available capped tools

- `get_sales`
- `get_invoices`
- `get_orders`
- `get_products`
- `get_customers`
- `get_warehouses`
- `get_inventory`
- `get_payments`

Successful responses use `{ ok: true, data: [...] }`. Errors use
`{ ok: false, error: "..." }`.
