# Creates (or replaces) the Windows scheduled task that runs pull-backup.ps1
# once a day.
#
# Run it once in PowerShell opened with "Run as administrator":
#   powershell -ExecutionPolicy Bypass -File install-backup-task.ps1
#
# 13:00 every day, when office computers are on; if the computer was off or
# asleep at 13:00, the run happens as soon as it is on again. Runs as SYSTEM:
# no window, works when nobody is logged in. (SYSTEM can't reach a network
# folder that needs a login -- for $SecondFolder use a second disk or a USB
# disk, or re-create the task under a user account in Task Scheduler.)

& {
    $Folder = "C:\mgmg-backup-script"   # the folder with pull-backup.ps1 -- change it if yours is elsewhere
    $TaskName = "MGMG database backup"
    $At = "13:00"

    $ErrorActionPreference = "Stop"
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pull-backup.ps1"))) { $Folder = $PSScriptRoot }
    $ScriptPath = Join-Path $Folder "pull-backup.ps1"

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warning "Open PowerShell with 'Run as administrator' and run this again."
        return
    }
    if (-not (Test-Path $ScriptPath)) {
        Write-Warning "pull-backup.ps1 not found in $Folder -- put it there or change `$Folder at the top."
        return
    }
    Unblock-File -Path $ScriptPath   # downloaded files are marked "from the internet"

    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`"" `
        -WorkingDirectory $Folder
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    # Catch up after the computer was off; run on battery; one copy at a time; stop a run hung for 2 hours.
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "Downloads the encrypted MGMG bot database backup once a day (kept outside Render)." -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    Write-Host "Task '$TaskName' created: every day at $At, first run started now."
    Write-Host "In a few minutes, check it:"
    Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName' | Format-List LastRunTime, LastTaskResult, NextRunTime"
    Write-Host "  Get-Content '$(Join-Path $Folder 'pull-backup.log')' -Tail 20"
    Write-Host "LastTaskResult: 0 = all good, 1 = a problem (see the log), 2 = settings at the top of the script not filled in, 267009 = still running."
}
