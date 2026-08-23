# Pushes open A/R invoices from the local SAP gateway to the Command
# Center's API, which does the actual database write. Nothing needs to be
# installed to run this -- it's plain PowerShell, same Invoke-RestMethod
# style already proven to work against the gateway on this machine.
#
# The gateway stays exactly as it is: loopback-only, unchanged. This script
# runs ON that same machine and only makes OUTBOUND connections (to the
# gateway, which is local, and to the Command Center's API, which is a
# normal outbound HTTPS call) -- nothing needs to accept inbound traffic on
# this machine for this to work.
#
# Fill in the three values below once, then run:
#   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1
#
# To run on a schedule: Windows Task Scheduler -> Create Task -> Action:
#   Program: powershell.exe
#   Arguments: -ExecutionPolicy Bypass -File "C:\path\to\push-ar-aging.ps1"

$GatewayUrl = "http://localhost:3000"          # or "http://[::1]:3000" if localhost doesn't resolve
$GatewayToken = "PASTE_GATEWAY_API_TOKEN_HERE"
$PushUrl = "https://PASTE_MGMG_API_HOST_HERE/webhooks/sap-push/PASTE_SAP_PUSH_WEBHOOK_SECRET_HERE"

$ErrorActionPreference = "Stop"

try {
    $gatewayHeaders = @{ Authorization = "Bearer $GatewayToken" }

    Write-Host "Fetching open invoices from $GatewayUrl ..."
    $result = Invoke-RestMethod -Method Post -Uri "$GatewayUrl/tools/get_invoices" `
        -Headers $gatewayHeaders -ContentType "application/json" -Body '{"limit":100}'

    if (-not $result.ok) {
        Write-Error "Gateway returned an error: $($result.error)"
        exit 1
    }

    $invoiceCount = $result.data.Count
    Write-Host "Got $invoiceCount invoice(s) from the gateway, pushing to Command Center ..."

    $pushBody = @{ invoices = $result.data } | ConvertTo-Json -Depth 10
    $pushResult = Invoke-RestMethod -Method Post -Uri $PushUrl -ContentType "application/json" -Body $pushBody

    if ($pushResult.ok) {
        Write-Host "Done: $($pushResult.written) row(s) written, $($pushResult.skipped) skipped."
    } else {
        Write-Error "Push rejected: $($pushResult.error)"
        exit 1
    }
} catch {
    Write-Error "FAILED: $($_.Exception.Message)"
    exit 1
}
