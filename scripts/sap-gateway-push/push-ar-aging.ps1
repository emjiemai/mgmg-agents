# Pushes data from the local SAP gateway to the Command Center's API, which
# does the actual database write. Nothing needs to be installed to run
# this -- it's plain PowerShell, same Invoke-RestMethod style already
# proven to work against the gateway on this machine.
#
# The gateway stays exactly as it is: loopback-only, unchanged. This script
# runs ON that same machine and only makes OUTBOUND connections (to the
# gateway, which is local, and to the Command Center's API, which is a
# normal outbound HTTPS call) -- nothing needs to accept inbound traffic on
# this machine for this to work.
#
# Covers all 8 gateway tools except get_sales (get_invoices already reads
# the same source table -- OINV -- with a strictly bigger interface, so
# get_sales would just be a duplicate pull of data already covered).
#
# Fill in the three values below once, then run:
#   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1
#
# To run on a schedule: Windows Task Scheduler -> Create Task -> Action:
#   Program: powershell.exe
#   Arguments: -ExecutionPolicy Bypass -File "C:\path\to\push-ar-aging.ps1"
# (same file as before -- if you already have a scheduled task pointed at
# this script, it picks up the new tools automatically next run, nothing
# to change in Task Scheduler itself.)

$GatewayUrl = "http://localhost:3000"          # or "http://[::1]:3000" if localhost doesn't resolve
$GatewayToken = "PASTE_GATEWAY_API_TOKEN_HERE"
$MgmgApiHost = "PASTE_MGMG_API_HOST_HERE"       # e.g. mgmg-api-eeky.onrender.com, no https:// prefix
$PushSecret = "PASTE_SAP_PUSH_WEBHOOK_SECRET_HERE"

$ErrorActionPreference = "Stop"
$gatewayHeaders = @{ Authorization = "Bearer $GatewayToken" }

function Push-Invoices {
    Write-Host "Fetching open invoices from $GatewayUrl ..."
    $result = Invoke-RestMethod -Method Post -Uri "$GatewayUrl/tools/get_invoices" `
        -Headers $gatewayHeaders -ContentType "application/json" -Body '{"limit":100}'

    if (-not $result.ok) {
        Write-Warning "get_invoices: gateway returned an error: $($result.error)"
        return
    }

    Write-Host "Got $($result.data.Count) invoice(s), pushing to Command Center ..."
    $pushUrl = "https://$MgmgApiHost/webhooks/sap-push/$PushSecret"
    $pushBody = @{ invoices = $result.data } | ConvertTo-Json -Depth 10
    $pushResult = Invoke-RestMethod -Method Post -Uri $pushUrl -ContentType "application/json" -Body $pushBody

    if ($pushResult.ok) {
        Write-Host "  invoices: $($pushResult.written) written, $($pushResult.skipped) skipped."
    } else {
        Write-Warning "  invoices push rejected: $($pushResult.error)"
    }
}

function Push-GatewayTool {
    param(
        [string]$GatewayTool,   # the gateway's own tool name, e.g. "get_orders"
        [string]$PushTool,      # matches push_handler.VALID_TOOLS, e.g. "orders"
        [string]$Body           # request body for the gateway call
    )

    Write-Host "Fetching from $GatewayUrl/tools/$GatewayTool ..."
    try {
        $result = Invoke-RestMethod -Method Post -Uri "$GatewayUrl/tools/$GatewayTool" `
            -Headers $gatewayHeaders -ContentType "application/json" -Body $Body
    } catch {
        Write-Warning "  $GatewayTool call failed: $($_.Exception.Message)"
        return
    }

    if (-not $result.ok) {
        Write-Warning "  $GatewayTool`: gateway returned an error: $($result.error)"
        return
    }

    $rowCount = $result.data.Count
    Write-Host "  got $rowCount row(s), pushing to Command Center ($PushTool) ..."
    $pushUrl = "https://$MgmgApiHost/webhooks/sap-gateway-push/$PushTool/$PushSecret"
    $pushBody = @{ rows = $result.data } | ConvertTo-Json -Depth 10
    try {
        $pushResult = Invoke-RestMethod -Method Post -Uri $pushUrl -ContentType "application/json" -Body $pushBody
    } catch {
        Write-Warning "  $PushTool push failed: $($_.Exception.Message)"
        return
    }

    if ($pushResult.ok) {
        Write-Host "  $PushTool`: $($pushResult.written) row(s) written."
    } else {
        Write-Warning "  $PushTool push rejected: $($pushResult.error)"
    }
}

try {
    Push-Invoices

    # Same 100-row cap the invoices pull already uses -- this is a periodic
    # background sync meant to keep a reasonably complete snapshot for
    # later questions, not a single interactive lookup (where a smaller,
    # targeted request would be the right call per the gateway's own
    # data-minimization guidance).
    Push-GatewayTool -GatewayTool "get_orders"     -PushTool "orders"     -Body '{"limit":100}'
    # get_products returned HTTP 400 at limit:100 in testing -- the teaching
    # doc's own example for this specific tool uses limit:20, unlike the
    # others which worked fine at 100, so it likely has a smaller enforced
    # cap. Lower here rather than guessing further; raise it back if a real
    # gateway response confirms a higher cap actually works.
    Push-GatewayTool -GatewayTool "get_products"   -PushTool "products"   -Body '{"limit":20}'
    Push-GatewayTool -GatewayTool "get_customers"  -PushTool "customers"  -Body '{"limit":100}'
    Push-GatewayTool -GatewayTool "get_warehouses" -PushTool "warehouses" -Body '{"limit":100}'
    Push-GatewayTool -GatewayTool "get_inventory"  -PushTool "inventory"  -Body '{"limit":100}'
    Push-GatewayTool -GatewayTool "get_payments"   -PushTool "payments"   -Body '{"limit":100}'

    Write-Host "All done."
} catch {
    Write-Error "FAILED: $($_.Exception.Message)"
    exit 1
}
