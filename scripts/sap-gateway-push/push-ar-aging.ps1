# Pushes SAP Business One data from the local SAP gateway to the Command
# Center (mgmg-api), which stores it for the morning brief, the receivables
# alert, the Billz -> SAP check and the Director's questions.
#
# Everything goes through the gateway's own tools (SAP_B1_AI_AGENT_TEACHING_
# UPDATED.md): no SQL here, no database password -- the gateway stays the
# only thing that talks to HANA. Plain Windows PowerShell 5.1, nothing to
# install. Only outbound connections: to the gateway (local) and to the
# Command Center (HTTPS).
#
# Two kinds of tools:
#   * today's tools (get_invoices, get_orders, ...) -- at most 100 rows each,
#     so the brief shows those numbers as "at least";
#   * three complete tools the gateway's maintainer adds
#     (docs/sap-gateway-tools.md): get_open_invoices, get_sales_by_date,
#     get_stock_value. Until one exists it is skipped; once it does, the
#     script uses it on its own -- nothing to change here.
#
# Run by hand:
#   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1
# Check the setup without pushing anything:
#   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1 -Check
# Schedule: Task Scheduler, every 30 minutes (README.md). Same file name as
# before, so an existing scheduled task keeps working.
# What happened is also written to push-ar-aging.log next to this file.

param([switch]$Check)

$GatewayUrl = "http://localhost:3000"              # or "http://[::1]:3000" if localhost doesn't resolve
$GatewayToken = "PASTE_GATEWAY_API_TOKEN_HERE"     # API_TOKEN from the gateway's .env
$MgmgApiHost = "PASTE_MGMG_API_HOST_HERE"          # e.g. mgmg-api-eeky.onrender.com, no https:// prefix
$PushSecret = "PASTE_SAP_PUSH_WEBHOOK_SECRET_HERE" # SAP_PUSH_WEBHOOK_SECRET from Render

$ErrorActionPreference = "Stop"
# Render accepts TLS 1.2+ only; older Windows PowerShell may start with TLS 1.0.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$gatewayHeaders = @{ Authorization = "Bearer $GatewayToken" }
$LogFile = Join-Path $PSScriptRoot "push-ar-aging.log"
$script:Failures = 0

# How many days of invoices get_sales_by_date is asked for: the Billz check
# looks two weeks back (a cheque never entered is reported until it is).
$SalesDays = 14

function Write-Log {
    param([string]$Text, [switch]$Warn)
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text
    if ($Warn) { Write-Warning $Text } else { Write-Host $Text }
    try {
        if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 2MB) {
            Move-Item -Force $LogFile "$LogFile.old"
        }
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    } catch { }
}

function Test-Filled {
    param([string]$Value)
    return [bool]($Value -and -not $Value.StartsWith("PASTE_"))
}

function Invoke-PushRequest {
    # Windows PowerShell 5.1 encodes a string -Body with the console's code
    # page, not UTF-8 (Cyrillic names arrived as "???????"), so the exact
    # UTF-8 bytes are sent instead.
    param([string]$Uri, [string]$JsonBody)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($JsonBody)
    return Invoke-RestMethod -Method Post -Uri $Uri -ContentType "application/json; charset=utf-8" -Body $bytes -TimeoutSec 300
}

function Invoke-GatewayTool {
    # Calls one gateway tool. Returns the tool's answer, or $null when the
    # gateway doesn't have this tool yet (HTTP 404, or 400 "unknown tool").
    param([string]$Tool, [string]$Body)

    try {
        return Invoke-RestMethod -Method Post -Uri "$GatewayUrl/tools/$Tool" `
            -Headers $gatewayHeaders -ContentType "application/json" -Body $Body -TimeoutSec 300
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) { throw }
        $status = [int]$response.StatusCode
        # Windows PowerShell 5.1 hides the body of a non-2xx answer: read it here.
        $text = ""
        try { $text = (New-Object System.IO.StreamReader($response.GetResponseStream())).ReadToEnd() } catch { }
        if ($status -eq 404 -or ($status -eq 400 -and $text -match "(?i)unknown|not found|no such")) { return $null }
        throw "HTTP $status $text"
    }
}

# --------------------------------------------------------- complete tools

function Push-CompleteTool {
    # One of the complete tools (docs/sap-gateway-tools.md): its rows go to
    # /webhooks/sap-data/<kind> for each kind listed. Returns $true when the
    # data reached the Command Center.
    param([string]$Tool, [string]$Body, [string[]]$Kinds, [int]$Limit = 0)

    try {
        $result = Invoke-GatewayTool -Tool $Tool -Body $Body
    } catch {
        Write-Log "  ${Tool}: gateway call failed: $($_.Exception.Message)" -Warn
        $script:Failures++
        return $false
    }
    if ($null -eq $result) {
        Write-Log "  ${Tool}: not in the gateway yet (docs/sap-gateway-tools.md) -- skipped."
        return $false
    }
    if (-not $result.ok) {
        Write-Log "  ${Tool}: gateway error: $($result.error)" -Warn
        $script:Failures++
        return $false
    }
    $rows = @()
    if ($null -ne $result.data) { $rows = @($result.data) }
    # A limited tool that filled its limit may have had more rows: not complete.
    $complete = -not ($Limit -gt 0 -and $rows.Count -ge $Limit)
    $body = ConvertTo-Json -InputObject @{ rows = $rows; complete = $complete } -Depth 6 -Compress
    $ok = $true
    foreach ($kind in $Kinds) {
        try {
            $push = Invoke-PushRequest -Uri "https://$MgmgApiHost/webhooks/sap-data/$kind/$PushSecret" -JsonBody $body
            if ($push.ok) {
                Write-Log "  ${Tool} -> ${kind}: $($rows.Count) read, $($push.written) stored."
            } else {
                Write-Log "  ${Tool} -> ${kind}: rejected: $($push.error)" -Warn
                $script:Failures++
                $ok = $false
            }
        } catch {
            Write-Log "  ${Tool} -> ${kind}: push failed: $($_.Exception.Message)" -Warn
            $script:Failures++
            $ok = $false
        }
    }
    return $ok
}

# ---------------------------------------------------------- today's tools

function Push-Invoices {
    try {
        $result = Invoke-GatewayTool -Tool "get_invoices" -Body '{"limit":100}'
        if ($null -eq $result -or -not $result.ok) {
            Write-Log "  get_invoices: gateway error: $($result.error)" -Warn
            $script:Failures++
            return
        }
        $body = @{ invoices = $result.data } | ConvertTo-Json -Depth 10
        $push = Invoke-PushRequest -Uri "https://$MgmgApiHost/webhooks/sap-push/$PushSecret" -JsonBody $body
        Write-Log "  get_invoices: $($push.written) written, $($push.skipped) skipped."
    } catch {
        Write-Log "  get_invoices failed: $($_.Exception.Message)" -Warn
        $script:Failures++
    }
}

function Push-GatewayTool {
    param([string]$Tool, [int]$Limit)

    try {
        $result = Invoke-GatewayTool -Tool "get_$Tool" -Body ('{"limit":' + $Limit + '}')
        if ($null -eq $result -or -not $result.ok) {
            Write-Log "  get_${Tool}: gateway error: $($result.error)" -Warn
            $script:Failures++
            return
        }
        $body = @{ rows = $result.data } | ConvertTo-Json -Depth 10
        $push = Invoke-PushRequest -Uri "https://$MgmgApiHost/webhooks/sap-gateway-push/$Tool/$PushSecret" -JsonBody $body
        Write-Log "  get_${Tool}: $(@($result.data).Count) read, $($push.written) written."
    } catch {
        Write-Log "  get_${Tool} failed: $($_.Exception.Message)" -Warn
        $script:Failures++
    }
}

# ------------------------------------------------------------------ check

function Test-Setup {
    Write-Host "Checking the setup (nothing is pushed) ..."
    try {
        $health = Invoke-RestMethod -Uri "https://$MgmgApiHost/health" -TimeoutSec 60
        Write-Host "  Command Center ($MgmgApiHost): reachable ($($health.status))."
    } catch {
        Write-Warning "  Command Center ($MgmgApiHost) not reachable: $($_.Exception.Message)"
    }
    try {
        Invoke-RestMethod -Uri "$GatewayUrl/health" -Headers $gatewayHeaders -TimeoutSec 60 | Out-Null
        Write-Host "  Gateway ($GatewayUrl): answers, token accepted."
    } catch {
        Write-Warning "  Gateway ($GatewayUrl): $($_.Exception.Message) -- is 'npm start' running, is the token right?"
        return
    }
    foreach ($tool in @("get_open_invoices", "get_sales_by_date", "get_stock_value")) {
        $body = '{}'
        if ($tool -eq "get_sales_by_date") {
            $day = (Get-Date).ToString("yyyy-MM-dd")
            $body = '{"from":"' + $day + '","to":"' + $day + '"}'
        }
        try {
            $result = Invoke-GatewayTool -Tool $tool -Body $body
            if ($null -eq $result) {
                Write-Host "  ${tool}: not added yet (docs/sap-gateway-tools.md)."
            } elseif ($result.ok) {
                Write-Host "  ${tool}: works ($(@($result.data).Count) rows)."
            } else {
                Write-Warning "  ${tool}: gateway error: $($result.error)"
            }
        } catch {
            Write-Warning "  ${tool}: $($_.Exception.Message)"
        }
    }
}

# ------------------------------------------------------------------- main

if (-not (Test-Filled $MgmgApiHost) -or -not (Test-Filled $PushSecret) -or -not (Test-Filled $GatewayToken)) {
    Write-Log "Fill in `$GatewayToken, `$MgmgApiHost and `$PushSecret at the top of this file first." -Warn
    exit 2
}

if ($Check) {
    Test-Setup
    exit 0
}

Write-Log "Pushing from the gateway ($GatewayUrl) to $MgmgApiHost ..."
try {
    # Complete tools first; each one that works replaces its capped stand-in.
    $openInvoices = Push-CompleteTool -Tool "get_open_invoices" -Body '{"limit":1000}' -Kinds @("ar_open") -Limit 1000
    $from = (Get-Date).AddDays(-$SalesDays).ToString("yyyy-MM-dd")
    $to = (Get-Date).ToString("yyyy-MM-dd")
    [void](Push-CompleteTool -Tool "get_sales_by_date" -Body ('{"from":"' + $from + '","to":"' + $to + '"}') -Kinds @("sales", "sales_lines"))
    [void](Push-CompleteTool -Tool "get_stock_value" -Body '{}' -Kinds @("stock_value"))

    # Today's tools: at most 100 rows each (get_products answers HTTP 400 above 20).
    if (-not $openInvoices) { Push-Invoices }
    Push-GatewayTool -Tool "orders" -Limit 100
    Push-GatewayTool -Tool "products" -Limit 20
    Push-GatewayTool -Tool "customers" -Limit 100
    Push-GatewayTool -Tool "warehouses" -Limit 100
    Push-GatewayTool -Tool "inventory" -Limit 100
    Push-GatewayTool -Tool "payments" -Limit 100
} catch {
    Write-Log "FAILED: $($_.Exception.Message)" -Warn
    exit 1
}

if ($script:Failures -gt 0) {
    Write-Log "Done with $($script:Failures) problem(s) -- see the warnings above." -Warn
    exit 1
}
Write-Log "All done."
