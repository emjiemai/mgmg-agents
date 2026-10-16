export const toolDefinitions = [
  {
    name: 'get_open_invoices',
    method: 'POST',
    path: '/tools/get_open_invoices',
    description: 'Get all currently open A/R invoices with their outstanding balance, in local, document (so\'m) and system currency, with the sales person\'s name.',
    input: { limit: 'integer 1-5000, default 1000' }
  },
  {
    name: 'get_sales_by_date',
    method: 'POST',
    path: '/tools/get_sales_by_date',
    description: 'Get A/R invoice and credit-note lines dated or entered in an inclusive date range (at most 92 days).',
    input: { from: 'required YYYY-MM-DD', to: 'required YYYY-MM-DD', limit: 'integer 1-50000, default 10000' }
  },
  {
    name: 'get_supplier_balances',
    method: 'POST',
    path: '/tools/get_supplier_balances',
    description: 'Get every supplier with a balance (supplier debt), in local, system (so\'m) and own currency, with the tax id.',
    input: { limit: 'integer 1-20000, default 5000' }
  },
  {
    name: 'get_stock_value',
    method: 'POST',
    path: '/tools/get_stock_value',
    description: 'Get current stock quantity, average price and calculated stock value by item and warehouse.',
    input: { limit: 'integer 1-50000, default 10000' }
  },
  {
    name: 'get_sales',
    method: 'POST',
    path: '/tools/get_sales',
    description: 'Get the latest non-cancelled A/R invoices as sales records.',
    input: { limit: 'integer 1-100, default 4' }
  },
  {
    name: 'get_invoices',
    method: 'POST',
    path: '/tools/get_invoices',
    description: 'Get recent A/R invoices or find an invoice by document number.',
    input: { limit: 'integer 1-100, default 20', doc_num: 'optional positive integer' }
  },
  {
    name: 'get_orders',
    method: 'POST',
    path: '/tools/get_orders',
    description: 'Get recent non-cancelled sales orders or find an order by document number.',
    input: { limit: 'integer 1-100, default 20', doc_num: 'optional positive integer' }
  },
  {
    name: 'get_products',
    method: 'POST',
    path: '/tools/get_products',
    description: 'Search or list SAP B1 item master records.',
    input: { limit: 'integer 1-100, default 20', search: 'optional item code or item name text' }
  },
  {
    name: 'get_customers',
    method: 'POST',
    path: '/tools/get_customers',
    description: 'Search or list customer business partners.',
    input: { limit: 'integer 1-100, default 20', search: 'optional customer code or name text' }
  },
  {
    name: 'get_warehouses',
    method: 'POST',
    path: '/tools/get_warehouses',
    description: 'List SAP B1 warehouses.',
    input: { limit: 'integer 1-100, default 100' }
  },
  {
    name: 'get_inventory',
    method: 'POST',
    path: '/tools/get_inventory',
    description: 'Get item stock by warehouse.',
    input: { limit: 'integer 1-200, default 50', item_code: 'optional exact item code', warehouse: 'optional exact warehouse code' }
  },
  {
    name: 'get_payments',
    method: 'POST',
    path: '/tools/get_payments',
    description: 'Get recent non-cancelled incoming payments.',
    input: { limit: 'integer 1-100, default 20' }
  },
  {
    name: 'get_customers_all',
    method: 'POST',
    path: '/tools/get_customers_all',
    description: 'Every customer with balance, group and sales person (no contact details).',
    input: { limit: 'integer 1-50000, default 20000' }
  },
  {
    name: 'get_items_all',
    method: 'POST',
    path: '/tools/get_items_all',
    description: 'Every item with group, stock, reorder levels (MinLevel/MaxLevel) and last purchase.',
    input: { limit: 'integer 1-100000, default 50000' }
  },
  {
    name: 'get_open_orders',
    method: 'POST',
    path: '/tools/get_open_orders',
    description: 'Every open sales order.',
    input: { limit: 'integer 1-50000, default 20000' }
  },
  {
    name: 'get_incoming_payments',
    method: 'POST',
    path: '/tools/get_incoming_payments',
    description: 'Incoming customer payments of the last N days.',
    input: { days: 'integer 1-400, default 45', limit: 'integer 1-50000, default 20000' }
  },
  {
    name: 'get_outgoing_payments',
    method: 'POST',
    path: '/tools/get_outgoing_payments',
    description: 'Outgoing supplier payments of the last N days.',
    input: { days: 'integer 1-400, default 45', limit: 'integer 1-50000, default 20000' }
  },
  {
    name: 'get_open_purchase_invoices',
    method: 'POST',
    path: '/tools/get_open_purchase_invoices',
    description: 'Every open supplier invoice with what is still to pay and its due date.',
    input: { limit: 'integer 1-50000, default 20000' }
  },
  {
    name: 'get_open_purchase_orders',
    method: 'POST',
    path: '/tools/get_open_purchase_orders',
    description: 'Every open purchase order line still to arrive.',
    input: { limit: 'integer 1-100000, default 50000' }
  },
  {
    name: 'get_sales_people',
    method: 'POST',
    path: '/tools/get_sales_people',
    description: 'Every sales employee (names for SlpCode).',
    input: { limit: 'integer 1-5000, default 1000' }
  },
  {
    name: 'get_equipment',
    method: 'POST',
    path: '/tools/get_equipment',
    description: 'Every customer equipment card.',
    input: { limit: 'integer 1-100000, default 50000' }
  },
  {
    name: 'get_service_calls',
    method: 'POST',
    path: '/tools/get_service_calls',
    description: 'Open service calls and those created in the last N days.',
    input: { days: 'integer 1-730, default 180', limit: 'integer 1-100000, default 50000' }
  },
  {
    name: 'get_service_contracts',
    method: 'POST',
    path: '/tools/get_service_contracts',
    description: 'Every service contract with its dates.',
    input: { limit: 'integer 1-50000, default 20000' }
  },
  {
    name: 'get_item_sales',
    method: 'POST',
    path: '/tools/get_item_sales',
    description: 'Quantity sold per item and warehouse in the last N days (invoices minus credit notes).',
    input: { days: 'integer 1-400, default 90', limit: 'integer 1-200000, default 100000' }
  }
];
