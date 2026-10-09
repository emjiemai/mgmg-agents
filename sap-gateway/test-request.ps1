# Run this while npm start is running.
# Replace YOUR_API_TOKEN with the value from .env.
$headers = @{ Authorization = "Bearer YOUR_API_TOKEN" }

Invoke-RestMethod `
  -Uri http://127.0.0.1:3000/health `
  -Headers $headers

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:3000/tools/get_sales `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"limit":4}' |
  ConvertTo-Json -Depth 10
