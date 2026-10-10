# Daily copy of the MGMG bot's database, kept OUTSIDE Render on this computer
# (the owner's IT instruction, IT-5: the 3-2-1 rule -- 3 copies, 2 kinds of
# storage, 1 away from the rest).
#
# The server makes the backup and encrypts it to the owner's public key
# (integrations/backup/dump.py). This computer only receives the encrypted
# file: without the owner's private key (in the password vault) nobody here
# can read it -- not IT either. Plain Windows PowerShell 5.1, nothing to
# install, only an outbound HTTPS connection to mgmg-api.
#
# Each run:
#   1. asks the server for a backup (it answers with the file's manifest:
#      time, size, SHA-256, row count per table -- counts only);
#   2. downloads the encrypted file and checks its SHA-256;
#   3. keeps it with its manifest in $Folder, and a copy in $SecondFolder;
#   4. removes old copies: every day for $KeepDays days, then the first copy
#      of each month for $KeepMonths months.
#
# Run by hand:
#   powershell -ExecutionPolicy Bypass -File pull-backup.ps1
# Check the setup without making a backup:
#   powershell -ExecutionPolicy Bypass -File pull-backup.ps1 -Check
# Schedule it once a day: install-backup-task.ps1 (README.md).
# What happened is also written to pull-backup.log next to this file.

param([switch]$Check)

$MgmgApiHost = "PASTE_MGMG_API_HOST_HERE"     # mgmg-api-eeky.onrender.com, no https:// prefix
$BackupSecret = "PASTE_BACKUP_SECRET_HERE"    # BACKUP_SECRET from Render (mgmg-shared)
$Folder = "D:\mgmg-backup"                    # where the copies are kept
$SecondFolder = ""                            # a second disk or USB disk, e.g. "E:\mgmg-backup" ("" = none)
$KeepDays = 30
$KeepMonths = 12

$ErrorActionPreference = "Stop"
# Render accepts TLS 1.2+ only; older Windows PowerShell may start with TLS 1.0.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$LogFile = Join-Path $PSScriptRoot "pull-backup.log"

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

function Get-HttpStatus {
    param($ErrorRecord)
    try { return [int]$ErrorRecord.Exception.Response.StatusCode } catch { return 0 }
}

function Remove-OldCopies {
    # Keeps every copy younger than $KeepDays days, and the first copy of each
    # month for $KeepMonths months; the rest go, each with its manifest.
    param([string]$Dir)
    if (-not (Test-Path $Dir)) { return 0 }
    $files = @(Get-ChildItem -Path $Dir -Filter "mgmg-db-*.dump.gpg" | Sort-Object Name)
    $firstOfMonth = @{}
    foreach ($f in $files) {
        $month = $f.Name.Substring(8, 7)
        if (-not $firstOfMonth.ContainsKey($month)) { $firstOfMonth[$month] = $f.Name }
    }
    $today = (Get-Date).Date
    $removed = 0
    foreach ($f in $files) {
        try { $day = [datetime]::ParseExact($f.Name.Substring(8, 10), "yyyy-MM-dd", $null) } catch { continue }
        $keep = ($today - $day).TotalDays -le $KeepDays
        if (-not $keep -and $firstOfMonth[$f.Name.Substring(8, 7)] -eq $f.Name) {
            $keep = $day -ge $today.AddMonths(-$KeepMonths)
        }
        if (-not $keep) {
            Remove-Item -Force $f.FullName
            Remove-Item -Force ($f.FullName -replace '\.dump\.gpg$', '.json') -ErrorAction SilentlyContinue
            $removed++
        }
    }
    return $removed
}

if (-not (Test-Filled $MgmgApiHost) -or -not (Test-Filled $BackupSecret)) {
    Write-Log "Fill in `$MgmgApiHost and `$BackupSecret at the top of this file first." -Warn
    exit 2
}
$Base = "https://$MgmgApiHost/backup/$BackupSecret"

if ($Check) {
    $problems = 0
    try {
        $s = Invoke-RestMethod -Uri $Base -Method Get -UseBasicParsing -TimeoutSec 60
        Write-Host ("Server: backups set up = {0}; public key = {1}; pg_dump = {2}; gpg = {3}" -f `
            $s.configured, $s.public_key, $s.tools.pg_dump, $s.tools.gpg)
        if (-not ($s.configured -and $s.tools.pg_dump -and $s.tools.gpg)) { $problems++ }
    } catch {
        if ((Get-HttpStatus $_) -eq 404) {
            Write-Warning "The server says 'not found': BACKUP_SECRET / BACKUP_PUBLIC_KEY aren't set in Render, or the secret here is wrong."
        } else {
            Write-Warning "The server didn't answer: $($_.Exception.Message)"
        }
        $problems++
    }
    foreach ($dir in @($Folder, $SecondFolder)) {
        if (-not $dir) { continue }
        try {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            $probe = Join-Path $dir ".write-test"
            Set-Content -Path $probe -Value "ok"
            Remove-Item -Force $probe
            $free = (Get-PSDrive -Name $dir.Substring(0, 1) -ErrorAction SilentlyContinue).Free
            $freeText = if ($free) { "{0:N1} GB free" -f ($free / 1GB) } else { "free space unknown" }
            Write-Host "Folder $dir`: writable, $freeText"
        } catch {
            Write-Warning "Folder $dir`: can't write there ($($_.Exception.Message))"
            $problems++
        }
    }
    if ($problems) { Write-Host "Check: $problems problem(s)."; exit 1 }
    Write-Host "Check: all good."
    exit 0
}

$started = Get-Date
# 1. The server makes the backup (a few seconds to a few minutes).
try {
    $m = Invoke-RestMethod -Uri $Base -Method Post -UseBasicParsing -TimeoutSec 1800
} catch {
    $code = Get-HttpStatus $_
    $why = if ($code -eq 404) { "not set up on the server, or the secret is wrong" }
           elseif ($code -eq 429) { "a backup was made a few minutes ago -- the next run will take one" }
           else { $_.Exception.Message }
    Write-Log "Backup not made: $why" -Warn
    exit 1
}
if (-not $m.ok) {
    Write-Log "Backup not made: $($m.error)" -Warn
    exit 1
}

# 2. Download it and check it arrived whole.
New-Item -ItemType Directory -Force -Path $Folder | Out-Null
$target = Join-Path $Folder $m.name
$partial = "$target.part"
try {
    Invoke-WebRequest -Uri "$Base/$($m.file)" -OutFile $partial -UseBasicParsing -TimeoutSec 3600
} catch {
    Remove-Item -Force $partial -ErrorAction SilentlyContinue
    Write-Log "Download failed: $($_.Exception.Message)" -Warn
    exit 1
}
$hash = (Get-FileHash -Algorithm SHA256 -Path $partial).Hash.ToLower()
if ($hash -ne $m.sha256) {
    Remove-Item -Force $partial
    Write-Log "Download damaged (SHA-256 differs) -- deleted; the next run takes a new one." -Warn
    exit 1
}
Move-Item -Force $partial $target
$manifestPath = $target -replace '\.dump\.gpg$', '.json'
$m | ConvertTo-Json -Depth 5 | Set-Content -Path $manifestPath -Encoding UTF8
$mb = "{0:N1}" -f ($m.bytes / 1MB)
Write-Log "Saved $($m.name): $mb MB, $(@($m.tables.PSObject.Properties).Count) tables, SHA-256 checked."

# 3. The second copy.
$failures = 0
if ($SecondFolder) {
    try {
        New-Item -ItemType Directory -Force -Path $SecondFolder | Out-Null
        Copy-Item -Force $target, $manifestPath -Destination $SecondFolder
        $copyHash = (Get-FileHash -Algorithm SHA256 -Path (Join-Path $SecondFolder $m.name)).Hash.ToLower()
        if ($copyHash -ne $m.sha256) { throw "the copy's SHA-256 differs" }
        Write-Log "Second copy: $SecondFolder"
    } catch {
        Write-Log "Second copy to $SecondFolder failed: $($_.Exception.Message)" -Warn
        $failures++
    }
} else {
    Write-Log "No second folder set (`$SecondFolder) -- only one copy on this computer." -Warn
}

# 4. Old copies.
foreach ($dir in @($Folder, $SecondFolder)) {
    if (-not $dir) { continue }
    try {
        $removed = Remove-OldCopies $dir
        if ($removed) { Write-Log "Removed $removed old copy(ies) from $dir" }
    } catch {
        Write-Log "Clean-up in $dir failed: $($_.Exception.Message)" -Warn
        $failures++
    }
}

$seconds = [int]((Get-Date) - $started).TotalSeconds
if ($failures) {
    Write-Log "Done with $failures problem(s) in $seconds s."
    exit 1
}
Write-Log "All done in $seconds s."
exit 0
