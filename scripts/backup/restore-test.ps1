# Restore test: proves a backup can really be brought back (the owner's IT
# instruction: IT-5 "restore test report", and once a month after that).
#
# Restores one encrypted backup into an EMPTY test database on this computer,
# compares every table's row count with the backup's manifest, writes a short
# report next to the backups (restore-test-<date>.txt -- counts only), then
# deletes the test database and the decrypted file. The company's data is
# never left readable on this computer.
#
# Needs, on this computer:
#   * Gpg4win, with the owner's PRIVATE backup key imported (Kleopatra) --
#     it asks for the key's passphrase from the password vault. Do this test
#     together with the owner, or with his written permission.
#   * PostgreSQL 16 (the EDB installer is fine; only the local server is used).
#
# Run (PowerShell, in this folder):
#   powershell -ExecutionPolicy Bypass -File restore-test.ps1
#   powershell -ExecutionPolicy Bypass -File restore-test.ps1 -BackupFile D:\mgmg-backup\mgmg-db-2026-10-12-1300.dump.gpg

param(
    [string]$BackupFile = "",
    [string]$Folder = "D:\mgmg-backup",
    [string]$PgBin = "C:\Program Files\PostgreSQL\16\bin",
    [string]$PgHost = "localhost",
    [int]$PgPort = 5432,
    [string]$PgUser = "postgres"
)

$ErrorActionPreference = "Stop"
$TestDb = "mgmg_restore_test"
# Every table with its exact row count -- the same query the server uses for the manifest.
$CountsSql = "SELECT t.table_name, (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', t.table_schema, t.table_name), false, true, '')))[1]::text::bigint FROM information_schema.tables t WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' ORDER BY 1"

foreach ($tool in @("pg_restore.exe", "psql.exe", "createdb.exe", "dropdb.exe")) {
    if (-not (Test-Path (Join-Path $PgBin $tool))) {
        Write-Warning "$tool not found in $PgBin -- install PostgreSQL 16 or pass -PgBin."
        exit 2
    }
}
if (-not (Get-Command gpg -ErrorAction SilentlyContinue)) {
    Write-Warning "gpg not found -- install Gpg4win and import the owner's backup key."
    exit 2
}
if (-not $BackupFile) {
    $newest = Get-ChildItem -Path $Folder -Filter "mgmg-db-*.dump.gpg" | Sort-Object Name | Select-Object -Last 1
    if (-not $newest) { Write-Warning "No backup in $Folder."; exit 2 }
    $BackupFile = $newest.FullName
}
$ManifestFile = $BackupFile -replace '\.dump\.gpg$', '.json'
if (-not (Test-Path $ManifestFile)) { Write-Warning "Manifest $ManifestFile not found."; exit 2 }
$manifest = Get-Content -Raw -Path $ManifestFile | ConvertFrom-Json

$started = Get-Date
$report = New-Object System.Collections.Generic.List[string]
$report.Add("MGMG database restore test -- $(Get-Date -Format 'yyyy-MM-dd HH:mm')")
$report.Add("Backup: $(Split-Path $BackupFile -Leaf) (made $($manifest.created), $([math]::Round($manifest.bytes / 1MB, 1)) MB)")
$failures = 0

$hash = (Get-FileHash -Algorithm SHA256 -Path $BackupFile).Hash.ToLower()
if ($hash -eq $manifest.sha256) { $report.Add("SHA-256: matches the manifest") }
else { $report.Add("SHA-256: DIFFERS from the manifest -- the file is damaged"); $failures++ }

$plain = Join-Path $env:TEMP ("mgmg-restore-" + [guid]::NewGuid().ToString("N") + ".dump")
$secure = Read-Host "Password of the local PostgreSQL user '$PgUser'" -AsSecureString
$env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$common = @("-h", $PgHost, "-p", "$PgPort", "-U", $PgUser)
try {
    Write-Host "Decrypting (gpg asks for the backup key's passphrase)..."
    & gpg --output $plain --decrypt $BackupFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $plain)) { throw "gpg could not decrypt the file (exit $LASTEXITCODE)" }
    $report.Add("Decrypted with the owner's key: yes")

    & (Join-Path $PgBin "dropdb.exe") @common --if-exists $TestDb
    & (Join-Path $PgBin "createdb.exe") @common $TestDb
    if ($LASTEXITCODE -ne 0) { throw "createdb failed (exit $LASTEXITCODE)" }

    Write-Host "Restoring into the empty test database '$TestDb'..."
    & (Join-Path $PgBin "pg_restore.exe") @common --no-owner --no-privileges --dbname $TestDb $plain
    $restoreExit = $LASTEXITCODE
    $report.Add("pg_restore: exit $restoreExit" + $(if ($restoreExit -eq 0) { " (no errors)" } else { " (see the window for its messages)" }))
    if ($restoreExit -ne 0) { $failures++ }

    $lines = & (Join-Path $PgBin "psql.exe") @common --dbname $TestDb --no-align --tuples-only --field-separator "|" --command $CountsSql
    if ($LASTEXITCODE -ne 0) { throw "psql could not count the tables (exit $LASTEXITCODE)" }
    $restored = @{}
    foreach ($line in $lines) {
        $parts = "$line".Split("|")
        if ($parts.Count -eq 2) { $restored[$parts[0]] = [long]$parts[1] }
    }
    $expected = @{}
    foreach ($p in $manifest.tables.PSObject.Properties) { $expected[$p.Name] = [long]$p.Value }

    $same = 0
    $diffs = New-Object System.Collections.Generic.List[string]
    foreach ($name in ($expected.Keys | Sort-Object)) {
        if (-not $restored.ContainsKey($name)) { $diffs.Add("  $name`: missing after restore (backup had $($expected[$name]) rows)") }
        elseif ($restored[$name] -ne $expected[$name]) { $diffs.Add("  $name`: $($restored[$name]) rows, backup had $($expected[$name])") }
        else { $same++ }
    }
    $report.Add("Tables: $($expected.Count) in the backup, $($restored.Count) restored, $same with the same row count")
    $report.Add("Rows: $(($restored.Values | Measure-Object -Sum).Sum) restored, $($manifest.rows) in the backup")
    if ($diffs.Count) {
        $report.Add("Differences:")
        foreach ($d in $diffs) { $report.Add($d) }
        $failures++
    }
} catch {
    $report.Add("STOPPED: $($_.Exception.Message)")
    $failures++
} finally {
    & (Join-Path $PgBin "dropdb.exe") @common --if-exists $TestDb
    Remove-Item -Force $plain -ErrorAction SilentlyContinue
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    $report.Add("Test database deleted, decrypted file deleted.")
}

$report.Add("Took $([int]((Get-Date) - $started).TotalSeconds) s.")
$report.Add($(if ($failures) { "RESULT: FAILED ($failures problem(s))" } else { "RESULT: PASSED -- the backup restores completely" }))
$report.Add("")
$report.Add("Done by: ____________________   Witness (owner): ____________________")
$out = Join-Path $Folder ("restore-test-" + (Get-Date -Format "yyyy-MM-dd") + ".txt")
$report | Set-Content -Path $out -Encoding UTF8
$report | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host "Report saved: $out"
if ($failures) { exit 1 }
exit 0
