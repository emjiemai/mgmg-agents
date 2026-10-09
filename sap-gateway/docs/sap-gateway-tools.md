# SAP gateway tools used by the Command Center sync

The gateway is the only component that talks to SAP HANA. The PowerShell
sync (`scripts/sap-gateway-push/push-ar-aging.ps1` in the Command Center)
calls these HTTP tools with the gateway's normal Bearer token.

All successful tool responses have this envelope:

```json
{
  "ok": true,
  "category": "...",
  "source": "...",
  "count": 1,
  "data": []
}
```

## Complete tools

### `POST /tools/get_open_invoices`

Request: `{"limit":1000}`

Non-cancelled A/R invoices whose `DocTotal - PaidToDate` is greater than
zero, with `BalanceDue`. Amounts in three currencies: `DocTotal` /
`PaidToDate` local (USD), `DocTotalFC` / `PaidFC` as written on the invoice
(`DocCur`, so'm), `DocTotalSy` / `PaidSys` system currency (so'm).
`SlpName` comes from OSLP. The sync treats a result smaller than the limit
as complete.

### `POST /tools/get_sales_by_date`

Request: `{"from":"2026-09-25","to":"2026-10-09"}` — inclusive, at most 92 days.

One row per line of every non-cancelled A/R invoice (`ObjType` 13,
OINV/INV1) and A/R credit note (`ObjType` 14, ORIN/RIN1) whose `DocDate`
**or** `CreateDate` is in the range — so a sale entered days late is still
returned. Header fields: `DocEntry`, `DocNum`, `CardCode`, `CardName`,
`DocDate`, `DocDueDate`, `CreateDate`, `CreateTS` (hhmmss), `DocTotal`,
`DocCur`, `DocTotalFC`, `DocTotalSy`, `DocStatus`, `CANCELED`, `SlpCode`,
`UserSign`; line fields: `LineNum`, `ItemCode`, `Dscription`, `CodeBars`,
`Quantity`, `Price`, `LineTotal`, `LineCurrency`, `WhsCode`.

### `POST /tools/get_stock_value`

Request: `{}`

Non-zero warehouse stock from `OITW` with the item's average price from
`OITM`, by item and warehouse; `StockValue = OnHand * AvgPrice`. (The
Command Center sums the rows per warehouse.)

### `POST /tools/get_supplier_balances`

Request: `{"limit":5000}`

Every supplier (OCRD, `CardType` 'S') with a non-zero balance: `CardCode`,
`CardName`, `LicTradNum` (tax id), `CardType`, `Currency`, `Balance`
(local, USD), `BalanceSys` (system currency, so'm — compared with 1C),
`BalanceFC` (the partner's currency). Money owed to a supplier is negative.

## Existing capped tools

These remain available for the fallback/brief sync:

- `get_invoices`
- `get_orders`
- `get_products`
- `get_customers`
- `get_warehouses`
- `get_inventory`
- `get_payments`

`get_products` uses SAP Business One's `OITM."InvntItem"` field.
