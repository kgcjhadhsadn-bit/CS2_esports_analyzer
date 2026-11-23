<#
.SYNOPSIS
    Starts the bot automatically when you sign in to Windows (Task Scheduler entry "cs2bot").

.DESCRIPTION
    Creates (or replaces) a Task Scheduler task that runs start.ps1 -Background at your logon:
      * runs as YOU, only while you are signed in (the bot needs your desktop to show its Betclic
        browser window), without administrator rights;
      * hidden console window (it may flash for a moment), output goes to logs\cs2bot-<date>.log;
      * waits -DelayMinutes after logon so Windows and Docker Desktop can settle;
      * restart on failure: start.ps1 itself restarts a crashed bot (30 s, 1, 2, 4, 8, 10 minutes;
        gives up after 5 crashes within an hour). The task's own "restart on failure" setting is
        set as well (3 times, every 5 minutes), but Windows only applies it when the task cannot be
        launched at all;
      * no time limit, keeps running on battery, normal process priority, never two copies.

    Autostart begins at sign-in, not at power-on. The PC must be on, signed in and awake before
    matches (see power-settings.ps1 and docs\WINDOWS_SETUP.md). Locking the screen is fine.

    Stop the running bot with stop.cmd; remove the entry with unregister-autostart.ps1.

.PARAMETER DelayMinutes
    Minutes to wait after logon before starting (default 1).

.PARAMETER Visible
    Run in a minimised PowerShell window instead of hidden (you can watch the output; closing that
    window stops the bot).

.PARAMETER StartNow
    Also start the task right away (does nothing if the bot already runs).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\register-autostart.ps1 -StartNow
#>
[CmdletBinding()]
param(
    [ValidateRange(0, 60)][int]$DelayMinutes = 1,
    [switch]$Visible,
    [switch]$StartNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

try {
    Assert-RepoReady
    if (-not (Get-Command -Name 'Register-ScheduledTask' -ErrorAction SilentlyContinue)) {
        throw 'The ScheduledTasks PowerShell module is missing (it ships with Windows 8 and newer).'
    }
    $userId = Get-CurrentUserId
    Write-Step "Creating the Task Scheduler entry '$AutostartTaskName' for $userId"

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
    if ($DelayMinutes -gt 0) { $trigger.Delay = "PT${DelayMinutes}M" }

    # ExecutionTimeLimit zero = no limit (the default would stop the bot after 3 days).
    # Priority 5 = normal (Task Scheduler's default 7 would run the bot below normal priority).
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 5 `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

    if ($Visible) {
        $action = New-ScriptTaskAction -ScriptName 'start.ps1' -ScriptArguments '-RestartOnCrash -NoBrowser' -Visible
    } else {
        $action = New-ScriptTaskAction -ScriptName 'start.ps1' -ScriptArguments '-Background'
    }

    Register-UserTask -TaskName $AutostartTaskName `
        -Description 'cs2bot: starts the CS2 betting bot at logon (deploy\windows\start.ps1). Stop it with deploy\windows\stop.cmd.' `
        -Action $action -Trigger $trigger -Settings $settings
    Write-Ok "Autostart registered: the bot starts $DelayMinutes minute(s) after you sign in."

    if ($StartNow) {
        if (Test-BotRunning) {
            Write-Info 'The bot is already running - it will start automatically from the next sign-in.'
        } else {
            Start-ScheduledTask -TaskName $AutostartTaskName
            Write-Ok "Started. The dashboard is ready in about a minute: $(Get-DashboardUrl)"
        }
    }

    Write-Host ''
    Write-Host 'Good to know:'
    Write-Host '  * Logs:            logs\cs2bot-<date>.log'
    Write-Host '  * Stop the bot:    deploy\windows\stop.cmd  (it is not restarted until the next sign-in)'
    Write-Host '  * Start it again:  deploy\windows\start.cmd, or: Start-ScheduledTask -TaskName cs2bot'
    Write-Host '  * Remove:          deploy\windows\unregister-autostart.ps1'
    Write-Host '  * Docker Desktop:  enable Settings > General > "Start Docker Desktop when you sign in"'
    Write-Host '                     (start.ps1 also starts it, but this is faster).'
    exit 0
} catch {
    Write-Fail $_.Exception.Message
    exit 1
}
