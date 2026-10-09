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
  }
];
