<#
.SYNOPSIS
    Removes the "start the bot at logon" Task Scheduler entry created by register-autostart.ps1.

.DESCRIPTION
    Only the Task Scheduler entry is removed; a bot that is running now keeps running (add
    -StopBot to stop it as well). With -All the optional entries of backup-db.ps1 -Schedule
    ("cs2bot-backup") and power-settings.ps1 ("cs2bot-wake") are removed too.

.PARAMETER StopBot
    Also stop the running bot (stop.ps1).

.PARAMETER All
    Also remove the daily backup and wake-up entries.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\unregister-autostart.ps1
#>
[CmdletBinding()]
param(
    [switch]$StopBot,
    [switch]$All
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

function Remove-TaskIfPresent {
    param([Parameter(Mandatory = $true)][string]$Name)
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Info "No Task Scheduler entry '$Name'."
        return
    }
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Ok "Removed the Task Scheduler entry '$Name'."
}

try {
    Write-Step 'Removing Task Scheduler entries'
    Remove-TaskIfPresent -Name $AutostartTaskName
    if ($All) {
        Remove-TaskIfPresent -Name 'cs2bot-backup'
        Remove-TaskIfPresent -Name $WakeTaskName
    }
    if ($StopBot) {
        & (Join-Path $PSScriptRoot 'stop.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'The bot could not be stopped (see above).' }
    } elseif (Test-BotRunning) {
        Write-Info 'The bot is still running. Stop it with deploy\windows\stop.cmd when you want.'
    }
    exit 0
} catch {
    Write-Fail $_.Exception.Message
    exit 1
}
