# Creates (or replaces) the Windows scheduled task that runs push-ar-aging.ps1
# every 30 minutes.
#
# Run it once in PowerShell opened with "Run as administrator" -- either paste
# this whole file in, or:
#   powershell -ExecutionPolicy Bypass -File install-task.ps1
#
# The task runs as SYSTEM: no window pops up, it runs whether or not anyone is
# logged in, and it keeps running on battery (the gateway machine is a laptop).

& {
    $Folder = "C:\mgmg-push"   # the folder with push-ar-aging.ps1 -- change it if yours is elsewhere
    $TaskName = "MGMG SAP push"

    $ErrorActionPreference = "Stop"
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "push-ar-aging.ps1"))) { $Folder = $PSScriptRoot }
    $ScriptPath = Join-Path $Folder "push-ar-aging.ps1"

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warning "Open PowerShell with 'Run as administrator' and run this again."
        return
    }
    if (-not (Test-Path $ScriptPath)) {
        Write-Warning "push-ar-aging.ps1 not found in $Folder -- put it there or change `$Folder at the top."
        return
    }
    Unblock-File -Path $ScriptPath   # downloaded files are marked "from the internet"

    # An older task that also runs the push script would push everything twice.
    foreach ($task in Get-ScheduledTask) {
        if ($task.TaskName -eq $TaskName) { continue }
        foreach ($a in $task.Actions) {
            if ("$($a.Arguments)" -like "*push-ar-aging*") {
                Write-Warning "Task '$($task.TaskName)' also runs push-ar-aging.ps1 -- remove it: Unregister-ScheduledTask -TaskName '$($task.TaskName)' -Confirm:`$false"
            }
        }
    }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`"" `
        -WorkingDirectory $Folder
    # Every 30 minutes from one minute from now, for ten years (the "forever" that every Windows version accepts).
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
    # Don't start a second copy while one runs; catch up after sleep; run on battery; stop a run hung for 20 minutes.
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "Pushes SAP data from the local gateway to the MGMG Command Center every 30 minutes." -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    Write-Host "Task '$TaskName' created: every 30 minutes, first run started now."
    Write-Host "In a minute, check it:"
    Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName' | Format-List LastRunTime, LastTaskResult, NextRunTime"
    Write-Host "  Get-Content '$(Join-Path $Folder 'push-ar-aging.log')' -Tail 20"
    Write-Host "LastTaskResult: 0 = all good, 1 = a problem (see the log), 2 = settings at the top of the script not filled in, 267009 = still running."
}
