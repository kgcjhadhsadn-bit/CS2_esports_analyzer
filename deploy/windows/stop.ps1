<#
.SYNOPSIS
    Stops the running bot cleanly (the same as pressing Ctrl+C in its window).

.DESCRIPTION
    1. Leaves a "stop requested" note for start.ps1 (so a supervised / autostarted bot is not
       restarted and Task Scheduler records a normal end).
    2. Sends Ctrl+C to the bot's console. "cs2bot up" then finishes the step it is doing, closes
       its browser windows and exits.
    3. Waits up to -TimeoutSeconds. If the bot is still running after that, it is ended forcefully
       (unless -NoForce). A forced end is safe for money: a bet whose final click was already made
       is recorded before the click and is reconciled with Betclic's bet history on the next start.

    PostgreSQL keeps running (it uses little memory). Add -StopDatabase to stop it too.

.PARAMETER TimeoutSeconds
    How long to wait for a clean stop (default 120).

.PARAMETER Force
    End the bot immediately, without asking it to stop cleanly first.

.PARAMETER NoForce
    Never end the bot forcefully; report a failure instead when it does not stop in time.

.PARAMETER StopDatabase
    Also stop the PostgreSQL container (docker compose stop). Data is kept.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\stop.ps1
#>
[CmdletBinding()]
param(
    [ValidateRange(5, 3600)][int]$TimeoutSeconds = 120,
    [switch]$Force,
    [switch]$NoForce,
    [switch]$StopDatabase
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

function Select-SignalTarget {
    # Any process attached to the bot's console works; python.exe is the bot itself.
    param([Parameter(Mandatory = $true)][object[]]$Processes)
    foreach ($name in @('python.exe', 'pythonw.exe', 'cs2bot.exe', 'uv.exe', 'cmd.exe')) {
        foreach ($process in $Processes) {
            if (([string]$process.Name).ToLowerInvariant() -eq $name) { return $process }
        }
    }
    return $Processes[0]
}

function Send-CtrlC {
    # Runs send-ctrl-c.ps1 in a separate PowerShell that has a console but no window
    # (CREATE_NO_WINDOW), so this window keeps its own console. Returns the helper's exit code.
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    $helper = Join-Path $PSScriptRoot 'send-ctrl-c.ps1'
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = Get-WindowsPowerShellPath
    $info.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$helper`" -ProcessId $ProcessId"
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $helperProcess = [System.Diagnostics.Process]::Start($info)
    if (-not $helperProcess.WaitForExit(30000)) {
        try { $helperProcess.Kill() } catch { $null = $_ }
        return 4
    }
    return $helperProcess.ExitCode
}

function Wait-BotGone {
    # True when no bot process is left within $Seconds.
    param([int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    $lastNote = Get-Date
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-BotRunning)) { return $true }
        if (((Get-Date) - $lastNote).TotalSeconds -ge 15) {
            $left = [int]($deadline - (Get-Date)).TotalSeconds
            Write-Info "Still shutting down... (up to $left more seconds)"
            $lastNote = Get-Date
        }
        Start-Sleep -Seconds 2
    }
    return (-not (Test-BotRunning))
}

function Stop-BotForcefully {
    foreach ($process in @(Get-BotProcess)) {
        Stop-ProcessTree -ProcessId ([int]$process.ProcessId)
    }
    return (Wait-BotGone -Seconds 20)
}

# ---------------------------------------------------------------------------------------------

try {
    Update-SessionPath
    Set-Location -LiteralPath $RepoRoot
    Initialize-RuntimeFolder -Path $RunDir
    if ($Force -and $NoForce) { throw 'Use either -Force or -NoForce, not both.' }

    # Read by start.ps1: "this stop was requested" (no restart after it, exit code 0). Written even
    # when no bot process is found: a supervising start.ps1 may be waiting to restart a crashed bot,
    # and this cancels that restart. start.ps1 deletes the note when it acts on it and when it
    # starts, so it is never left in force.
    [System.IO.File]::WriteAllText($StopMarker, (Get-Date).ToString('o'))

    $processes = @(Get-BotProcess)
    if ($processes.Count -eq 0) {
        Write-Host 'The bot is not running.' -ForegroundColor Green
    } else {
        $stopped = $false
        if (-not $Force) {
            $target = Select-SignalTarget -Processes $processes
            Write-Step "Asking the bot to stop (Ctrl+C to process $($target.ProcessId), $($target.Name))"
            $code = Send-CtrlC -ProcessId ([int]$target.ProcessId)
            if ($code -ne 0) {
                Write-Warn "Could not send Ctrl+C (helper exit code $code)."
            } else {
                Write-Info "Waiting up to $TimeoutSeconds seconds for a clean shutdown..."
                $stopped = Wait-BotGone -Seconds $TimeoutSeconds
            }
        }
        if (-not $stopped) {
            if ($NoForce) {
                throw "The bot is still running after $TimeoutSeconds seconds. Run stop.ps1 -Force to end it."
            }
            Write-Step 'Ending the bot forcefully'
            $stopped = Stop-BotForcefully
            if (-not $stopped) {
                throw 'Some bot processes could not be ended. Close the bot window, or restart Windows.'
            }
        }
        Write-Ok 'The bot has stopped.'
    }

    if ($StopDatabase) {
        Write-Step 'Stopping PostgreSQL (data is kept)'
        if (Test-DockerRunning) {
            Invoke-Checked 'docker compose stop' 'docker' @('compose', '-f', $ComposeFile, 'stop')
            Write-Ok 'PostgreSQL stopped.'
        } else {
            Write-Info 'Docker is not running - nothing to stop.'
        }
    }
    exit 0
} catch {
    Write-Fail $_.Exception.Message
    exit 1
}
