# Install a Windows Scheduled Task that runs the Etsy auto pipeline daily.
#
# Usage (right-click > "Run with PowerShell", or run from any shell):
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_scheduler.ps1
#
# Options:
#   -Daily            Create a task that runs `python scheduler.py --once`
#                     every day at 09:00 (uses the built-in weekday niche
#                     rotation). (default)
#   -LongRunning      Create a task that starts `python scheduler.py` (the
#                     24/7 loop) when you log on, instead of a daily one-shot.
#   -Niche "<seed>"   Fixed niche for the daily task instead of the weekday
#                     rotation (one-shot mode and workflow_dispatch use this).
#   -Time "09:00"     Hour of the day for the daily trigger.
#   -Uninstall        Remove the scheduled task.
#
# Notes:
#   - Default tasks run only while you are logged on (no password stored).
#     To run at 09:00 even when logged out you must supply -User <account>
#     and -Password, e.g.:
#       .\scripts\install_windows_scheduler.ps1 -User "$env:USERDOMAIN\$env:USERNAME" -Password "***"
#   - Environment variables used by main.py/scheduler.py are read from the
#     .env file inside the project, so keep `.env` beside `main.py`.

param(
    [switch]$Daily,
    [switch]$LongRunning,
    [switch]$Uninstall,
    [string]$Niche = "",
    [string]$Time = "09:00",
    [string]$TaskName = "EtsyAutoDaily",
    [string]$User = "",
    [string]$Password = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Find-Python {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $ver = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($ver) { return $ver.Trim() }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { return $python.Source }
    throw "Python 3 not found. Install it and add `python` to PATH."
}

function Remove-Task {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    } else {
        Write-Host "No scheduled task '$TaskName' found." -ForegroundColor Yellow
    }
}

if ($Uninstall) { Remove-Task; exit 0 }

if ($Daily -and $LongRunning) {
    throw "Choose either -Daily or -LongRunning, not both."
}

$Python = Find-Python
$Scheduler = Join-Path $ProjectRoot "scheduler.py"
if (-not (Test-Path $Scheduler)) {
    throw "scheduler.py not found at '$Scheduler'."
}

# Build the python command + arguments.
if ($LongRunning) {
    $ArgList = """$Scheduler"""
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Description = "Starts the EtsyAuto 24/7 scheduler loop (daily 09:00 run + niche rotation)."
} else {
    $Extra = if ($Niche) { "--niche ""$Niche""" } else { "" }
    $ArgList = """$Scheduler"" --once $Extra"
    $Trigger = New-ScheduledTaskTrigger -Daily -At $Time
    $Description = "Runs the EtsyAuto pipeline once per day at $Time (weekday niche rotation; dry-run unless Etsy tokens validate)."
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $ArgList `
    -WorkingDirectory $ProjectRoot

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal -UserId "INTERACTIVE" -LogonType Interactive -RunLevel Limited
if ($User) {
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Password -RunLevel Limited
}

$registerParams = @{
    TaskName    = $TaskName
    Action      = $Action
    Trigger     = $Trigger
    Settings    = $Settings
    Principal   = $Principal
    Description = $Description
    Force       = $true
}
if ($User -and $Password) {
    $registerParams["User"] = $User
    $registerParams["Password"] = $Password
}
Register-ScheduledTask @registerParams | Out-Null

Write-Host "Installed task '$TaskName'." -ForegroundColor Green
Write-Host "  Python     : $Python"
Write-Host "  Scheduler  : $Scheduler"
Write-Host "  Working dir: $ProjectRoot"
Write-Host "  Trigger    : $($Trigger.CimClass.CimClassName) ($Time)"
Write-Host ""
Write-Host "Verify with:" -NoNewline
Write-Host "  Get-ScheduledTask -TaskName $TaskName | FL *"
Write-Host "Run it manually now:" -NoNewline
Write-Host "  Start-ScheduledTask -TaskName $TaskName"