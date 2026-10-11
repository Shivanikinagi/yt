# Registers a daily Windows Task Scheduler job (07:00 local) that runs run_daily.ps1.
# Usage (PowerShell, no admin needed for a per-user task):
#   powershell -ExecutionPolicy Bypass -File scheduler\register_windows_task.ps1
param([string]$Time = "07:00", [string]$TaskName = "TechShortsFactory")
$Script = Join-Path $PSScriptRoot "run_daily.ps1"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Host "Registered task '$TaskName' daily at $Time. It runs only while this PC is on (WakeToRun wakes it from sleep, not from shutdown)."
Write-Host "Remove with: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
