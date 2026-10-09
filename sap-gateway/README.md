# SAP Business One AI Gateway

Local read-only Node.js gateway between SAP Business One on HANA and an AI
agent / Command Center.

## Architecture

`AI / PowerShell -> http://localhost:3000 -> SAP HANA`

The gateway is the only component that contains the HANA credentials and
executes SQL. The PowerShell sync never connects directly to HANA.

## Start

1. Copy `.env.example` to `.env`.
2. Set `HANA_SERVER`, `HANA_PORT`, `HANA_USER`, `HANA_PASSWORD`,
   `HANA_SCHEMA`.
3. Set a strong `API_TOKEN`.
4. Run:

```powershell
npm install
npm start
```

## Test

```powershell
$headers = @{ Authorization = "Bearer YOUR_API_TOKEN" }

Invoke-RestMethod `
  -Uri http://127.0.0.1:3000/health `
  -Headers $headers

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:3000/tools/get_open_invoices `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"limit":1000}' |
  ConvertTo-Json -Depth 10
```

The complete tools required by the Command Center sync are documented
in `docs/sap-gateway-tools.md`. This copy of the gateway is kept in the
Command Center repository (`sap-gateway/`); after changing it, copy `src/`
to the gateway computer and restart `npm start`.

Values (dates, codes) are passed to HANA as bound parameters (`exec(sql,
params)` in `src/hana.js`), never pasted into the SQL text.

## Security

Use a dedicated read-only SAP HANA user. Keep the gateway bound to
`127.0.0.1` unless there is a deliberate reason to expose it on a trusted
network.
