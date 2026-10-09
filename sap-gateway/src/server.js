import express from 'express';
import helmet from 'helmet';
import rateLimit from 'express-rate-limit';
import dotenv from 'dotenv';
import { connect, disconnect, exec, HANA_SCHEMA } from './hana.js';
import { requireApiToken } from './auth.js';
import { getSales } from './sales.js';
import { getProducts } from './products.js';
import { getCustomers } from './customers.js';
import { getWarehouses } from './warehouses.js';
import { getInventory } from './inventory.js';
import { getOrders } from './orders.js';
import { getInvoices } from './invoices.js';
import { getPayments } from './payments.js';
import { getOpenInvoices } from './open-invoices.js';
import { getSalesByDate } from './sales-by-date.js';
import { getStockValue } from './stock-value.js';
import { getSupplierBalances } from './supplier-balances.js';
import { toolDefinitions } from './tools.js';

dotenv.config();

const app = express();
const HOST = process.env.HOST || '127.0.0.1';
const PORT = Number.parseInt(process.env.PORT || '3000', 10);

if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) {
  throw new Error('PORT must be a valid TCP port.');
}

app.disable('x-powered-by');
app.use(helmet());
app.use(express.json({ limit: '32kb' }));
app.use(rateLimit({
  windowMs: 60_000,
  limit: 60,
  standardHeaders: 'draft-8',
  legacyHeaders: false,
  message: { ok: false, error: 'Too many requests' }
}));

app.use(requireApiToken);

app.get('/health', async (_req, res) => {
  try {
    const result = await exec('SELECT CURRENT_USER AS "CURRENT_USER" FROM DUMMY');
    return res.json({ ok: true, hana: true, user: result[0]?.CURRENT_USER ?? null });
  } catch {
    return res.status(503).json({ ok: false, hana: false, error: 'HANA connection/query failed' });
  }
});

app.get('/tools', (_req, res) => {
  res.json({ ok: true, server: 'sap-b1-ai-gateway', tools: toolDefinitions });
});

const toolHandler = (fn) => async (req, res) => {
  try {
    const result = await fn(req.body ?? {});
    return res.json({ ok: true, ...result });
  } catch (error) {
    console.error('Tool error:', error.message);
    return res.status(400).json({ ok: false, error: error.message });
  }
};

app.post('/tools/get_sales', toolHandler(getSales));
app.post('/tools/get_invoices', toolHandler(getInvoices));
app.post('/tools/get_orders', toolHandler(getOrders));
app.post('/tools/get_products', toolHandler(getProducts));
app.post('/tools/get_customers', toolHandler(getCustomers));
app.post('/tools/get_warehouses', toolHandler(getWarehouses));
app.post('/tools/get_inventory', toolHandler(getInventory));
app.post('/tools/get_payments', toolHandler(getPayments));
app.post('/tools/get_open_invoices', toolHandler(getOpenInvoices));
app.post('/tools/get_sales_by_date', toolHandler(getSalesByDate));
app.post('/tools/get_stock_value', toolHandler(getStockValue));
app.post('/tools/get_supplier_balances', toolHandler(getSupplierBalances));

app.use((_req, res) => {
  res.status(404).json({ ok: false, error: 'Not found' });
});

async function start() {
  try {
    await connect();
    console.log('========================================');
    console.log('Connected to SAP HANA');
    console.log(`Schema: ${HANA_SCHEMA}`);
    console.log('========================================');
    app.listen(PORT, HOST, () => {
      console.log(`SAP B1 AI Gateway: http://${HOST}:${PORT}`);
    });
  } catch (error) {
    console.error('HANA connection failed:', error.message || error);
    process.exit(1);
  }
}

function shutdown() {
  console.log('\nShutting down...');
  disconnect();
  process.exit(0);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
start();
