# Pushes SAP Business One data to the Command Center (mgmg-api), which stores
# it. Plain Windows PowerShell 5.1 -- nothing to install for the script itself.
#
# Two ways to read SAP, chosen by what is filled in below:
#
#   1. DATABASE (full data, 2026-10-03) -- straight from SAP's HANA database
#      with a read-only user, through the SAP HANA ODBC driver (HDBODBC, part
#      of the SAP HANA client that the SAP B1 client already needs). Every
#      row, no 100-row cap, and tables the gateway has no tool for: invoice
#      lines, purchase invoices, purchase orders, equipment cards, service
#      calls. 16 kinds, one POST each (see $Datasets below).
#
#   2. GATEWAY (the old way) -- the local SAP gateway's tools, at most 100
#      rows each (products 20). Used only while the database settings are
#      still "PASTE_...".
#
# Only OUTBOUND connections: to SAP's database / the local gateway, and to
# the Command Center over HTTPS. Nothing here accepts inbound traffic.
#
# Run by hand:
#   powershell -ExecutionPolicy Bypass -File push-ar-aging.ps1
# Schedule (Task Scheduler, every 30 minutes) -- see README.md. The file name
# is unchanged, so an existing scheduled task keeps working.
# What happened is also written to push-ar-aging.log next to this file.

# --- Command Center (always) ---
$MgmgApiHost = "PASTE_MGMG_API_HOST_HERE"            # e.g. mgmg-api-eeky.onrender.com (no https://)
$PushSecret  = "PASTE_SAP_PUSH_WEBHOOK_SECRET_HERE"  # SAP_PUSH_WEBHOOK_SECRET from Render

# --- SAP HANA database (full data) ---
$HanaServer   = "PASTE_HANA_HOST_AND_PORT_HERE"      # e.g. 192.168.1.10:30015 -- the same server the gateway uses
$HanaUser     = "PASTE_READONLY_USER_HERE"           # a read-only user (README.md: how to create one)
$HanaPassword = "PASTE_PASSWORD_HERE"
$HanaSchema   = "MGM"                                # the SAP company database (schema)
$HanaDriver   = "HDBODBC"                            # 64-bit SAP HANA ODBC driver

# --- Old gateway (only while the database settings above are not filled in) ---
$GatewayUrl   = "http://localhost:3000"              # or "http://[::1]:3000" if localhost doesn't resolve
$GatewayToken = "PASTE_GATEWAY_API_TOKEN_HERE"

$ErrorActionPreference = "Stop"
$LogFile = Join-Path $PSScriptRoot "push-ar-aging.log"
$script:Failures = 0

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

# ---------------------------------------------------------------- database

# One entry per kind of data: the name the Command Center stores it under
# (integrations/sap/push_handler.py FULL_DATASETS) and the SELECT that reads
# it. "{S}" is replaced with $HanaSchema. Dates go out as text (YYYY-MM-DD).
# Every column was checked against the SAP export of 2026-10-02
# (sap_columns.json; scripts/selfcheck.py checks it again on every change).
$Datasets = [ordered]@{

    # OSLP -- sales people (first: open invoices are labelled with their names)
    "sales_people" = @'
SELECT T0."SlpCode", T0."SlpName", T0."Active"
FROM "{S}"."OSLP" T0
'@

    # OINV -- every open A/R invoice (customer debt), with what's paid so far
    "ar_open" = @'
SELECT T0."DocEntry", T0."DocNum", T0."CardCode", T0."CardName",
       TO_VARCHAR(T0."DocDate", 'YYYY-MM-DD') AS "DocDate",
       TO_VARCHAR(T0."DocDueDate", 'YYYY-MM-DD') AS "DocDueDate",
       T0."DocStatus", T0."CANCELED", T0."DocCur", T0."DocTotal", T0."PaidToDate",
       T0."DocTotalFC", T0."PaidFC", T0."SlpCode"
FROM "{S}"."OINV" T0
WHERE T0."DocStatus" = 'O' AND T0."CANCELED" = 'N'
'@

    # OINV + ORIN -- A/R invoices and credit notes of the last 45 days
    # (dated or entered), for sales and the Billz check
    "sales" = @'
SELECT 13 AS "ObjType", T0."DocEntry", T0."DocNum", T0."CardCode", T0."CardName",
       TO_VARCHAR(T0."DocDate", 'YYYY-MM-DD') AS "DocDate",
       TO_VARCHAR(T0."CreateDate", 'YYYY-MM-DD') AS "CreateDate", T0."CreateTS",
       T0."DocStatus", T0."CANCELED", T0."DocCur", T0."DocRate", T0."DocTotal", T0."DocTotalFC",
       T0."DocTotalSy", T0."VatSum", T0."SlpCode", T0."UserSign"
FROM "{S}"."OINV" T0
WHERE T0."DocDate" >= ADD_DAYS(CURRENT_DATE, -45) OR T0."CreateDate" >= ADD_DAYS(CURRENT_DATE, -45)
UNION ALL
SELECT 14 AS "ObjType", T1."DocEntry", T1."DocNum", T1."CardCode", T1."CardName",
       TO_VARCHAR(T1."DocDate", 'YYYY-MM-DD') AS "DocDate",
       TO_VARCHAR(T1."CreateDate", 'YYYY-MM-DD') AS "CreateDate", T1."CreateTS",
       T1."DocStatus", T1."CANCELED", T1."DocCur", T1."DocRate", T1."DocTotal", T1."DocTotalFC",
       T1."DocTotalSy", T1."VatSum", T1."SlpCode", T1."UserSign"
FROM "{S}"."ORIN" T1
WHERE T1."DocDate" >= ADD_DAYS(CURRENT_DATE, -45) OR T1."CreateDate" >= ADD_DAYS(CURRENT_DATE, -45)
'@

    # INV1 + RIN1 -- the lines of those documents (what was sold, from which warehouse)
    "sales_lines" = @'
SELECT 13 AS "ObjType", T1."DocEntry", T1."LineNum", T1."ItemCode", T1."Dscription", T1."Quantity",
       T1."Price", T1."Currency", T1."LineTotal", T1."TotalFrgn", T1."GTotal", T1."WhsCode", T1."CodeBars", T1."SlpCode"
FROM "{S}"."INV1" T1
INNER JOIN "{S}"."OINV" T0 ON T0."DocEntry" = T1."DocEntry"
WHERE T0."DocDate" >= ADD_DAYS(CURRENT_DATE, -45) OR T0."CreateDate" >= ADD_DAYS(CURRENT_DATE, -45)
UNION ALL
SELECT 14 AS "ObjType", T3."DocEntry", T3."LineNum", T3."ItemCode", T3."Dscription", T3."Quantity",
       T3."Price", T3."Currency", T3."LineTotal", T3."TotalFrgn", T3."GTotal", T3."WhsCode", T3."CodeBars", T3."SlpCode"
FROM "{S}"."RIN1" T3
INNER JOIN "{S}"."ORIN" T2 ON T2."DocEntry" = T3."DocEntry"
WHERE T2."DocDate" >= ADD_DAYS(CURRENT_DATE, -45) OR T2."CreateDate" >= ADD_DAYS(CURRENT_DATE, -45)
'@

    # OITW + OITM -- stock per item and warehouse (every non-zero row)
    "inventory" = @'
SELECT T0."ItemCode", T1."ItemName", T0."WhsCode", T0."OnHand", T0."IsCommited", T0."OnOrder",
       T0."AvgPrice", T0."StockValue", T0."MinStock", T1."ItmsGrpCod", T1."CodeBars",
       TO_VARCHAR(T1."LastPurDat", 'YYYY-MM-DD') AS "LastPurDat"
FROM "{S}"."OITW" T0
INNER JOIN "{S}"."OITM" T1 ON T1."ItemCode" = T0."ItemCode"
WHERE T0."OnHand" <> 0 OR T0."IsCommited" <> 0 OR T0."OnOrder" <> 0
'@

    # OITM + OITB -- every item, with its group
    "products" = @'
SELECT T0."ItemCode", T0."ItemName", T0."CodeBars", T0."ItmsGrpCod", T1."ItmsGrpNam",
       T0."InvntItem", T0."SellItem", T0."PrchseItem", T0."ManSerNum", T0."OnHand", T0."AvgPrice",
       T0."LastPurPrc", T0."LastPurCur", TO_VARCHAR(T0."LastPurDat", 'YYYY-MM-DD') AS "LastPurDat",
       T0."MinLevel", T0."LeadTime", T0."validFor", T0."frozenFor"
FROM "{S}"."OITM" T0
LEFT JOIN "{S}"."OITB" T1 ON T1."ItmsGrpCod" = T0."ItmsGrpCod"
'@

    # OCRD + OCRG -- every business partner (customers, suppliers), no phones or e-mails
    "customers" = @'
SELECT T0."CardCode", T0."CardName", T0."CardType", T0."GroupCode", T1."GroupName", T0."City",
       T0."SlpCode", T0."Balance", T0."CreditLine", T0."Currency", T0."validFor", T0."frozenFor",
       TO_VARCHAR(T0."CreateDate", 'YYYY-MM-DD') AS "CreateDate"
FROM "{S}"."OCRD" T0
LEFT JOIN "{S}"."OCRG" T1 ON T1."GroupCode" = T0."GroupCode"
'@

    # OWHS -- warehouses
    "warehouses" = @'
SELECT T0."WhsCode", T0."WhsName", T0."Inactive", T0."Locked"
FROM "{S}"."OWHS" T0
'@

    # ORCT -- incoming payments, last 45 days
    "payments" = @'
SELECT T0."DocEntry", T0."DocNum", TO_VARCHAR(T0."DocDate", 'YYYY-MM-DD') AS "DocDate",
       T0."CardCode", T0."CardName", T0."DocType", T0."DocCurr", T0."DocTotal", T0."DocTotalFC",
       T0."CashSum", T0."TrsfrSum", T0."CheckSum", T0."CreditSum", T0."Canceled"
FROM "{S}"."ORCT" T0
WHERE T0."DocDate" >= ADD_DAYS(CURRENT_DATE, -45)
'@

    # OVPM -- outgoing payments, last 45 days
    "payments_out" = @'
SELECT T0."DocEntry", T0."DocNum", TO_VARCHAR(T0."DocDate", 'YYYY-MM-DD') AS "DocDate",
       T0."CardCode", T0."CardName", T0."DocType", T0."DocCurr", T0."DocTotal", T0."DocTotalFC",
       T0."CashSum", T0."TrsfrSum", T0."CheckSum", T0."CreditSum", T0."Canceled"
FROM "{S}"."OVPM" T0
WHERE T0."DocDate" >= ADD_DAYS(CURRENT_DATE, -45)
'@

    # ORDR -- open sales orders
    "orders" = @'
SELECT T0."DocEntry", T0."DocNum", T0."CardCode", T0."CardName",
       TO_VARCHAR(T0."DocDate", 'YYYY-MM-DD') AS "DocDate",
       TO_VARCHAR(T0."DocDueDate", 'YYYY-MM-DD') AS "DocDueDate",
       T0."DocStatus", T0."CANCELED", T0."DocCur", T0."DocTotal", T0."DocTotalFC", T0."SlpCode"
FROM "{S}"."ORDR" T0
WHERE T0."DocStatus" = 'O' AND T0."CANCELED" = 'N'
'@

    # OPCH -- open purchase (A/P) invoices: what we owe suppliers, and when
    "ap_open" = @'
SELECT T0."DocEntry", T0."DocNum", T0."CardCode", T0."CardName",
       TO_VARCHAR(T0."DocDate", 'YYYY-MM-DD') AS "DocDate",
       TO_VARCHAR(T0."DocDueDate", 'YYYY-MM-DD') AS "DocDueDate",
       T0."DocCur", T0."DocTotal", T0."PaidToDate", T0."DocTotalFC", T0."PaidFC"
FROM "{S}"."OPCH" T0
WHERE T0."DocStatus" = 'O' AND T0."CANCELED" = 'N'
'@

    # OPOR + POR1 -- open purchase order lines: what is ordered and not yet received
    "po_open" = @'
SELECT T0."DocEntry", T0."DocNum", T0."CardCode", T0."CardName",
       TO_VARCHAR(T0."DocDate", 'YYYY-MM-DD') AS "DocDate",
       TO_VARCHAR(T0."DocDueDate", 'YYYY-MM-DD') AS "DocDueDate", T0."DocCur",
       T1."LineNum", T1."ItemCode", T1."Dscription", T1."Quantity", T1."OpenQty", T1."Price",
       T1."Currency", T1."LineTotal", T1."WhsCode", TO_VARCHAR(T1."ShipDate", 'YYYY-MM-DD') AS "ShipDate"
FROM "{S}"."OPOR" T0
INNER JOIN "{S}"."POR1" T1 ON T1."DocEntry" = T0."DocEntry"
WHERE T0."DocStatus" = 'O' AND T0."CANCELED" = 'N' AND T1."LineStatus" = 'O'
'@

    # OINS -- customer equipment cards (which customer has which machine, serial numbers)
    "equipment" = @'
SELECT T0."insID", T0."customer", T0."custmrName", T0."itemCode", T0."itemName", T0."manufSN",
       T0."internalSN", T0."status", TO_VARCHAR(T0."dlvryDate", 'YYYY-MM-DD') AS "dlvryDate",
       T0."deliveryNo", T0."invoiceNum", T0."city", TO_VARCHAR(T0."createDate", 'YYYY-MM-DD') AS "createDate"
FROM "{S}"."OINS" T0
'@

    # OSCL -- service calls
    "service_calls" = @'
SELECT T0."callID", T0."DocNum", T0."subject", T0."customer", T0."custmrName", T0."itemCode",
       T0."itemName", T0."internalSN", T0."manufSN", T0."status", T0."priority", T0."callType",
       T0."problemTyp", T0."origin", T0."assignee", T0."technician",
       TO_VARCHAR(T0."createDate", 'YYYY-MM-DD') AS "createDate", T0."createTime",
       TO_VARCHAR(T0."closeDate", 'YYYY-MM-DD') AS "closeDate", T0."insID", T0."contractID"
FROM "{S}"."OSCL" T0
'@

    # OCTR -- service contracts
    "service_contracts" = @'
SELECT T0."ContractID", T0."CstmrCode", T0."CstmrName",
       TO_VARCHAR(T0."StartDate", 'YYYY-MM-DD') AS "StartDate",
       TO_VARCHAR(T0."EndDate", 'YYYY-MM-DD') AS "EndDate", T0."Status", T0."CntrcType",
       TO_VARCHAR(T0."TermDate", 'YYYY-MM-DD') AS "TermDate"
FROM "{S}"."OCTR" T0
'@
}

function Read-Rows {
    # Runs one SELECT and returns its rows as plain objects ready for JSON.
    param([System.Data.Odbc.OdbcConnection]$Connection, [string]$Sql)

    $command = $Connection.CreateCommand()
    $command.CommandText = $Sql
    $command.CommandTimeout = 300
    $adapter = New-Object System.Data.Odbc.OdbcDataAdapter($command)
    $table = New-Object System.Data.DataTable
    [void]$adapter.Fill($table)

    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($dataRow in $table.Rows) {
        $row = [ordered]@{}
        foreach ($column in $table.Columns) {
            $value = $dataRow[$column.ColumnName]
            if ($value -is [System.DBNull]) { $value = $null }
            elseif ($value -is [datetime]) { $value = $value.ToString("yyyy-MM-dd HH:mm:ss") }
            $row[$column.ColumnName] = $value
        }
        $rows.Add([pscustomobject]$row)
    }
    return ,$rows
}

function Push-Dataset {
    param([System.Data.Odbc.OdbcConnection]$Connection, [string]$Name, [string]$Sql)

    try {
        $rows = Read-Rows -Connection $Connection -Sql $Sql.Replace("{S}", $HanaSchema)
    } catch {
        Write-Log "  ${Name}: SAP query failed: $($_.Exception.Message)" -Warn
        $script:Failures++
        return
    }
    try {
        $body = ConvertTo-Json -InputObject @{ rows = $rows.ToArray() } -Depth 4 -Compress
        $result = Invoke-PushRequest -Uri "https://$MgmgApiHost/webhooks/sap-data/$Name/$PushSecret" -JsonBody $body
    } catch {
        Write-Log "  ${Name}: push failed: $($_.Exception.Message)" -Warn
        $script:Failures++
        return
    }
    if ($result.ok) {
        Write-Log "  ${Name}: $($rows.Count) read, $($result.written) stored."
    } else {
        Write-Log "  ${Name}: rejected by the Command Center: $($result.error)" -Warn
        $script:Failures++
    }
}

function Push-FromDatabase {
    Write-Log "Reading SAP's database ($HanaServer, $HanaSchema) ..."
    $connection = New-Object System.Data.Odbc.OdbcConnection(
        "Driver={$HanaDriver};ServerNode=$HanaServer;UID=$HanaUser;PWD={$HanaPassword}"
    )
    try {
        $connection.Open()
    } catch {
        Write-Log "Could not connect to SAP's database: $($_.Exception.Message)" -Warn
        Write-Log "Check: the SAP HANA client (ODBC driver '$HanaDriver') is installed, and the server, user and password above." -Warn
        $script:Failures++
        return
    }
    try {
        foreach ($name in $Datasets.Keys) {
            Push-Dataset -Connection $connection -Name $name -Sql $Datasets[$name]
        }
    } finally {
        $connection.Close()
    }
}

# ----------------------------------------------------------------- gateway

function Push-FromGateway {
    Write-Log "Database settings not filled in -- using the gateway ($GatewayUrl), max 100 rows per kind."
    $headers = @{ Authorization = "Bearer $GatewayToken" }

    try {
        $result = Invoke-RestMethod -Method Post -Uri "$GatewayUrl/tools/get_invoices" `
            -Headers $headers -ContentType "application/json" -Body '{"limit":100}'
        if ($result.ok) {
            $body = @{ invoices = $result.data } | ConvertTo-Json -Depth 10
            $push = Invoke-PushRequest -Uri "https://$MgmgApiHost/webhooks/sap-push/$PushSecret" -JsonBody $body
            Write-Log "  invoices: $($push.written) written, $($push.skipped) skipped."
        } else {
            Write-Log "  get_invoices: gateway error: $($result.error)" -Warn
            $script:Failures++
        }
    } catch {
        Write-Log "  invoices failed: $($_.Exception.Message)" -Warn
        $script:Failures++
    }

    # get_products answers HTTP 400 above 20 rows; the rest take 100.
    $tools = [ordered]@{ orders = 100; products = 20; customers = 100; warehouses = 100; inventory = 100; payments = 100 }
    foreach ($tool in $tools.Keys) {
        try {
            $result = Invoke-RestMethod -Method Post -Uri "$GatewayUrl/tools/get_$tool" `
                -Headers $headers -ContentType "application/json" -Body ('{"limit":' + $tools[$tool] + '}')
            if (-not $result.ok) {
                Write-Log "  get_${tool}: gateway error: $($result.error)" -Warn
                $script:Failures++
                continue
            }
            $body = @{ rows = $result.data } | ConvertTo-Json -Depth 10
            $push = Invoke-PushRequest -Uri "https://$MgmgApiHost/webhooks/sap-gateway-push/$tool/$PushSecret" -JsonBody $body
            Write-Log "  ${tool}: $($result.data.Count) read, $($push.written) written."
        } catch {
            Write-Log "  ${tool} failed: $($_.Exception.Message)" -Warn
            $script:Failures++
        }
    }
}

# -------------------------------------------------------------------- main

if (-not (Test-Filled $MgmgApiHost) -or -not (Test-Filled $PushSecret)) {
    Write-Log "Fill in `$MgmgApiHost and `$PushSecret at the top of this file first." -Warn
    exit 2
}

if ((Test-Filled $HanaServer) -and (Test-Filled $HanaUser) -and (Test-Filled $HanaPassword)) {
    Push-FromDatabase
} elseif (Test-Filled $GatewayToken) {
    Push-FromGateway
} else {
    Write-Log "Fill in either the SAP database settings or the gateway token at the top of this file." -Warn
    exit 2
}

if ($script:Failures -gt 0) {
    Write-Log "Done with $($script:Failures) problem(s) -- see the warnings above." -Warn
    exit 1
}
Write-Log "All done."
