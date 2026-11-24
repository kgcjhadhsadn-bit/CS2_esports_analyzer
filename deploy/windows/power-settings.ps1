<#
.SYNOPSIS
    Shows the Windows sleep settings that matter for the bot and, only if you say yes, changes them.

.DESCRIPTION
    The bot only works while the PC is on and awake. Pre-match bets must be placed in the hours
    before each match, and the odds and data jobs need to run before that.

    What already happens without this script: while start.ps1 runs the bot, it asks Windows not to
    go to sleep on its own (like a video player does). The screen may still turn off; that is fine.
    That request does NOT stop sleep when you close a laptop lid, press the power button or choose
    Sleep, and it does not wake a PC that is already asleep.

    Options offered here (each one asks first; nothing changes without your "y"):
      1. Never sleep / hibernate when on mains power (AC). Trade-off: more electricity
         (a desktop PC idling uses roughly 30-80 W, i.e. about 20-60 kWh per month) and the PC is
         reachable longer. The current values are saved so option 2 can restore them.
      2. Restore the values saved by option 1.
      3. Allow "wake timers" on mains power (needed for option 4).
      4. Wake the PC from sleep every day at a fixed time and start the bot (Task Scheduler entry
         "cs2bot-wake"; you stay signed in while the PC sleeps). Trade-off: the PC switches itself
         on at that time, also when you did not plan it. Some PCs with "Modern Standby" ignore wake
         timers; test it once.
      5. Remove the daily wake entry.

    Battery (DC) settings are only shown, never changed.

.PARAMETER ShowOnly
    Only show the settings, do not offer changes.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\power-settings.ps1
#>
[CmdletBinding()]
param(
    [switch]$ShowOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$PowerBackupFile = Join-Path $RunDir 'power-settings-backup.json'

function Get-PowerIndex {
    # Current AC and DC values of one power setting, read from "powercfg /query". The labels of
    # that output are translated (Polish Windows), so only the hex numbers are used: the last two
    # "0x..." values are always the current AC and DC index. Returns @{AC; DC} ($null = unknown).
    param(
        [Parameter(Mandatory = $true)][string]$Subgroup,
        [Parameter(Mandatory = $true)][string]$Setting
    )
    $result = @{ AC = $null; DC = $null }
    $text = Get-NativeOutput 'powercfg' @('/query', 'SCHEME_CURRENT', $Subgroup, $Setting)
    if ($null -eq $text) { return $result }
    $found = @([regex]::Matches($text, '0x([0-9a-fA-F]{1,8})\b') | ForEach-Object { $_.Groups[1].Value })
    if ($found.Count -ge 2) {
        $result.AC = [Convert]::ToInt64($found[$found.Count - 2], 16)
        $result.DC = [Convert]::ToInt64($found[$found.Count - 1], 16)
    }
    return $result
}

function Format-Timeout {
    param($Seconds)
    if ($null -eq $Seconds) { return 'unknown' }
    if ($Seconds -eq 0) { return 'never' }
    if ($Seconds % 3600 -eq 0) { return ('after {0} h' -f ($Seconds / 3600)) }
    return ('after {0} min' -f [Math]::Round($Seconds / 60))
}

function Format-WakeTimers {
    param($Value)
    switch ($Value) {
        0 { return 'disabled' }
        1 { return 'enabled' }
        2 { return 'important wake timers only' }
        default { return 'unknown' }
    }
}

function Show-PowerSettings {
    Write-Step 'Current power settings (active power plan)'
    $scheme = Get-NativeOutput 'powercfg' @('/getactivescheme')
    if ($null -ne $scheme) { Write-Info $scheme }
    $sleep = Get-PowerIndex 'SUB_SLEEP' 'STANDBYIDLE'
    $hibernate = Get-PowerIndex 'SUB_SLEEP' 'HIBERNATEIDLE'
    $wake = Get-PowerIndex 'SUB_SLEEP' 'RTCWAKE'
    Write-Info ('Sleep:        on mains power {0,-16} on battery {1}' -f (Format-Timeout $sleep.AC), (Format-Timeout $sleep.DC))
    Write-Info ('Hibernate:    on mains power {0,-16} on battery {1}' -f (Format-Timeout $hibernate.AC), (Format-Timeout $hibernate.DC))
    Write-Info ('Wake timers:  on mains power {0,-16} on battery {1}' -f (Format-WakeTimers $wake.AC), (Format-WakeTimers $wake.DC))
    $wakeTask = Get-ScheduledTask -TaskName $WakeTaskName -ErrorAction SilentlyContinue
    if ($null -eq $wakeTask) {
        Write-Info "Daily wake-up: none (Task Scheduler entry '$WakeTaskName' not present)"
    } else {
        $at = ''
        foreach ($trigger in @($wakeTask.Triggers)) {
            if ($trigger.StartBoundary) { $at = ([datetime]$trigger.StartBoundary).ToString('HH:mm') }
        }
        Write-Info "Daily wake-up: at $at (Task Scheduler entry '$WakeTaskName')"
    }
    if (Test-Path -LiteralPath $PowerBackupFile) {
        Write-Info "Saved earlier values: $PowerBackupFile (option 2 restores them)"
    }
    Write-Host ''
    Write-Info 'While the bot runs (start.ps1), Windows does not go to sleep on its own anyway.'
    Write-Info 'For details run "powercfg /requests" or "powercfg /waketimers" as Administrator.'
}

function Set-AcIndex {
    param(
        [Parameter(Mandatory = $true)][string]$Setting,
        [Parameter(Mandatory = $true)][long]$Value
    )
    Invoke-Checked "powercfg $Setting" 'powercfg' @('/setacvalueindex', 'SCHEME_CURRENT', 'SUB_SLEEP', $Setting, [string]$Value)
}

function Save-PowerBackup {
    if (Test-Path -LiteralPath $PowerBackupFile) {
        Write-Info 'Values saved earlier are kept (they are the ones from before the first change).'
        return
    }
    $sleep = Get-PowerIndex 'SUB_SLEEP' 'STANDBYIDLE'
    $hibernate = Get-PowerIndex 'SUB_SLEEP' 'HIBERNATEIDLE'
    if ($null -eq $sleep.AC) {
        throw 'Could not read the current sleep setting, so it cannot be saved. Nothing was changed.'
    }
    # Hibernate may be unreadable (hibernation switched off with "powercfg /h off"): saved as null
    # and then left alone on restore.
    $data = @{ standby_ac_s = $sleep.AC; hibernate_ac_s = $hibernate.AC; saved_at = (Get-Date).ToString('o') }
    Initialize-RuntimeFolder -Path $RunDir
    [System.IO.File]::WriteAllText($PowerBackupFile, ($data | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok "Current values saved to $PowerBackupFile"
}

function Set-NeverSleepOnAc {
    Write-Host ''
    Write-Host 'Never sleep / hibernate on mains power:' -ForegroundColor Cyan
    Write-Host '  + the bot keeps working even when start.ps1 is not running yet / at night.'
    Write-Host '  - higher electricity use; the screen still turns off as before.'
    if (-not (Confirm-Action 'Change these settings now?' -Default $false)) { Write-Info 'Nothing changed.'; return }
    Save-PowerBackup
    Set-AcIndex -Setting 'STANDBYIDLE' -Value 0
    try {
        Set-AcIndex -Setting 'HIBERNATEIDLE' -Value 0
    } catch {
        # Hibernation switched off on this PC: there is no hibernate timeout to change.
        Write-Info 'Hibernate timeout not changed (hibernation seems to be switched off on this PC).'
    }
    Invoke-Checked 'powercfg /setactive' 'powercfg' @('/setactive', 'SCHEME_CURRENT')
    Write-Ok 'On mains power the PC no longer goes to sleep or hibernates on its own.'
}

function Restore-PowerBackup {
    if (-not (Test-Path -LiteralPath $PowerBackupFile)) { Write-Info 'There are no saved values to restore.'; return }
    $data = [System.IO.File]::ReadAllText($PowerBackupFile) | ConvertFrom-Json
    $names = @($data.PSObject.Properties | ForEach-Object { $_.Name })
    if ($names -notcontains 'standby_ac_s' -or $null -eq $data.standby_ac_s) {
        throw "$PowerBackupFile does not contain the saved sleep value. Nothing was changed."
    }
    $hibernateSaved = ($names -contains 'hibernate_ac_s' -and $null -ne $data.hibernate_ac_s)
    $hibernateText = 'not saved'
    if ($hibernateSaved) { $hibernateText = Format-Timeout ([long]$data.hibernate_ac_s) }
    Write-Info ('Saved values: sleep {0}, hibernate {1} (on mains power)' -f (Format-Timeout ([long]$data.standby_ac_s)), $hibernateText)
    if (-not (Confirm-Action 'Restore them now?' -Default $false)) { Write-Info 'Nothing changed.'; return }
    Set-AcIndex -Setting 'STANDBYIDLE' -Value ([long]$data.standby_ac_s)
    if ($hibernateSaved) { Set-AcIndex -Setting 'HIBERNATEIDLE' -Value ([long]$data.hibernate_ac_s) }
    Invoke-Checked 'powercfg /setactive' 'powercfg' @('/setactive', 'SCHEME_CURRENT')
    Remove-Item -LiteralPath $PowerBackupFile -Force
    Write-Ok 'Previous sleep settings restored.'
}

function Enable-WakeTimersOnAc {
    Write-Host ''
    Write-Host 'Wake timers let scheduled tasks (option 4, Windows Update) wake the PC from sleep.' -ForegroundColor Cyan
    if (-not (Confirm-Action 'Allow wake timers on mains power?' -Default $false)) { Write-Info 'Nothing changed.'; return }
    Set-AcIndex -Setting 'RTCWAKE' -Value 1
    Invoke-Checked 'powercfg /setactive' 'powercfg' @('/setactive', 'SCHEME_CURRENT')
    Write-Ok 'Wake timers are allowed on mains power.'
}

function Register-WakeTask {
    $time = Read-Host 'Wake the PC every day at (HH:mm, e.g. 10:30; matches usually start between 11:00 and 22:00 Warsaw time)'
    if ($time -notmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { Write-Warn 'Not a valid time - nothing changed.'; return }
    Write-Host ''
    Write-Host "At $time every day Windows wakes the PC (if it sleeps) and starts the bot if it is not running." -ForegroundColor Cyan
    Write-Host '  - the PC switches itself on at that time; it only works while you stay signed in (sleep, not shut down).'
    if (-not (Confirm-Action 'Create this daily wake-up?' -Default $false)) { Write-Info 'Nothing changed.'; return }
    $wake = Get-PowerIndex 'SUB_SLEEP' 'RTCWAKE'
    if ($wake.AC -ne 1) { Write-Warn 'Wake timers are not enabled on mains power - use option 3 as well, or the PC will not wake.' }
    $parts = $time.Split(':')
    $at = (Get-Date).Date.AddHours([int]$parts[0]).AddMinutes([int]$parts[1])
    $trigger = New-ScheduledTaskTrigger -Daily -At $at
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 5
    $action = New-ScriptTaskAction -ScriptName 'start.ps1' -ScriptArguments '-Background'
    Register-UserTask -TaskName $WakeTaskName -Description 'cs2bot: wakes the PC daily and starts the bot (deploy\windows\power-settings.ps1).' `
        -Action $action -Trigger $trigger -Settings $settings
    Write-Ok "Daily wake-up at $time registered (Task Scheduler entry '$WakeTaskName'). Test it once: sleep the PC a few minutes before."
}

function Unregister-WakeTask {
    $task = Get-ScheduledTask -TaskName $WakeTaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { Write-Info 'There is no daily wake-up entry.'; return }
    if (-not (Confirm-Action "Remove the Task Scheduler entry '$WakeTaskName'?" -Default $false)) { Write-Info 'Nothing changed.'; return }
    Unregister-ScheduledTask -TaskName $WakeTaskName -Confirm:$false
    Write-Ok 'Daily wake-up removed.'
}

# ---------------------------------------------------------------------------------------------

try {
    if ($env:OS -ne 'Windows_NT') { throw 'This script is for Windows.' }
    if (-not (Test-CommandExists 'powercfg')) { throw 'powercfg was not found (it is part of Windows).' }
    Set-Location -LiteralPath $RepoRoot
    Show-PowerSettings
    if ($ShowOnly) { exit 0 }

    while ($true) {
        Write-Host ''
        Write-Host 'What would you like to do? Nothing is changed without asking.' -ForegroundColor Cyan
        Write-Host '  1  Never sleep / hibernate on mains power (saves the current values first)'
        Write-Host '  2  Restore the saved sleep values'
        Write-Host '  3  Allow wake timers on mains power'
        Write-Host '  4  Wake the PC every day at a fixed time and start the bot'
        Write-Host '  5  Remove the daily wake-up'
        Write-Host '  6  Show the settings again'
        Write-Host '  Q  Quit'
        $choice = (Read-Host 'Choice').Trim().ToUpperInvariant()
        try {
            switch ($choice) {
                '1' { Set-NeverSleepOnAc }
                '2' { Restore-PowerBackup }
                '3' { Enable-WakeTimersOnAc }
                '4' { Register-WakeTask }
                '5' { Unregister-WakeTask }
                '6' { Show-PowerSettings }
                'Q' { exit 0 }
                '' { exit 0 }
                default { Write-Warn "Unknown choice '$choice'." }
            }
        } catch {
            Write-Fail $_.Exception.Message
            Write-Info 'If Windows refused the change, open PowerShell with "Run as administrator" and try again.'
        }
    }
} catch {
    Write-Fail $_.Exception.Message
    exit 1
}
