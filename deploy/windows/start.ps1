<#
.SYNOPSIS
    Starts the bot: Docker Desktop -> PostgreSQL -> "uv run cs2bot up" (dashboard + scheduler).

.DESCRIPTION
    1. Does nothing (exit 0) when the bot is already running.
    2. Starts Docker Desktop if needed, starts PostgreSQL and waits until it is healthy.
    3. Applies pending database migrations (cs2bot db upgrade; a no-op when there are none).
    4. Runs "uv run cs2bot up" and copies everything it prints to logs\cs2bot-YYYY-MM-DD.log
       (one file per day; files older than -LogRetentionDays are deleted).
    5. While the bot runs, Windows is asked not to go to sleep on its own (the screen may still
       turn off). The request ends when the bot stops; no power setting is changed. Use -AllowSleep
       to switch this off.
    6. With -Background or -RestartOnCrash: when the bot crashes (exits with an error without
       having been asked to stop), it is started again after 30 s, 1, 2, 4, 8 and 10 minutes. After
       -MaxRestartsPerHour crashes within one hour the script gives up (see the log file).
       Task Scheduler's own "restart on failure" does not help here: it only reacts when a task
       cannot be started at all, not when the program it started exits with an error.

    Stop the bot with Ctrl+C in this window, or with stop.cmd / stop.ps1 from another window.
    The window can be minimised. Closing it stops the bot abruptly (prefer Ctrl+C).

.PARAMETER Background
    For the Task Scheduler entries (register-autostart.ps1, the wake task of power-settings.ps1):
    no console output, no browser tab, waits longer for Docker after logon, restarts after crashes.

.PARAMETER RestartOnCrash
    Restart the bot after a crash also when running in a visible window.

.PARAMETER MaxRestartsPerHour
    Give up after this many crash restarts within one hour (default 5).

.PARAMETER AllowSleep
    Do not keep the PC awake while the bot runs.

.PARAMETER NoBrowser
    Do not open the dashboard in the default browser.

.PARAMETER LogRetentionDays
    Delete log files older than this many days (default 30).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\start.ps1
#>
[CmdletBinding()]
param(
    [switch]$Background,
    [switch]$RestartOnCrash,
    [ValidateRange(0, 100)][int]$MaxRestartsPerHour = 5,
    [switch]$AllowSleep,
    [switch]$NoBrowser,
    [ValidateRange(1, 3650)][int]$LogRetentionDays = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:LogWriter = $null
$script:LogDate = ''
$script:PendingRead = $null
$script:DashboardOpened = $false
$script:DashboardPort = 8000
$script:StopRequested = $false

function Remove-OldLogs {
    $cutoff = (Get-Date).AddDays(-$LogRetentionDays)
    $old = @(Get-ChildItem -LiteralPath $LogDir -Filter 'cs2bot-*.log' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff })
    foreach ($file in $old) {
        Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
    }
}

function Write-BotLog {
    # Appends one line to today's log file (switching files at midnight) and echoes it to the
    # console unless -Background. Uses .NET calls only, so it also works while PowerShell is
    # shutting the script down after Ctrl+C.
    param([string]$Line)
    $today = (Get-Date).ToString('yyyy-MM-dd')
    if ($today -ne $script:LogDate) {
        if ($null -ne $script:LogWriter) { $script:LogWriter.Dispose() }
        $path = Join-Path $LogDir "cs2bot-$today.log"
        try {
            $script:LogWriter = New-Object System.IO.StreamWriter($path, $true, $Utf8NoBom)
        } catch {
            # Today's file is locked by another program: write to a separate file instead.
            $path = Join-Path $LogDir "cs2bot-$today-$PID.log"
            $script:LogWriter = New-Object System.IO.StreamWriter($path, $true, $Utf8NoBom)
        }
        $script:LogWriter.AutoFlush = $true
        $script:LogDate = $today
        Remove-OldLogs
    }
    $script:LogWriter.WriteLine($Line)
    if (-not $Background) { [Console]::WriteLine($Line) }
}

function Write-BotLogMarker {
    # "===== ... =====" lines that make the start / stop of each run easy to find in the log.
    param([string]$Text)
    Write-BotLog ("===== {0} {1} =====" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'), $Text)
}

function Invoke-Logged {
    # Runs a command line through cmd.exe (which merges error output into normal output at the OS
    # level, so PowerShell 5.1 never turns a warning on stderr into an error) and copies every line
    # into the log. Returns the exit code.
    param([Parameter(Mandatory = $true)][string]$CommandLine)
    $ErrorActionPreference = 'Continue'
    & $env:ComSpec /d /c "$CommandLine 2>&1" | ForEach-Object { Write-BotLog ([string]$_) }
    return $LASTEXITCODE
}

function Enable-KeepAwake {
    # Asks Windows not to enter sleep on idle for as long as this script lives
    # (ES_CONTINUOUS | ES_SYSTEM_REQUIRED). The display is not kept on. Nothing is stored: the
    # request disappears when this process ends. Check with: powercfg /requests (as Administrator).
    try {
        if (-not ('Cs2Bot.Power' -as [type])) {
            Add-Type -Namespace 'Cs2Bot' -Name 'Power' -MemberDefinition '[DllImport("kernel32.dll", SetLastError = true)] public static extern uint SetThreadExecutionState(uint esFlags);'
        }
        [void][Cs2Bot.Power]::SetThreadExecutionState([uint32]2147483649)
        return $true
    } catch {
        return $false
    }
}

function Disable-KeepAwake {
    try {
        if ('Cs2Bot.Power' -as [type]) {
            [void][Cs2Bot.Power]::SetThreadExecutionState([uint32]2147483648)
        }
    } catch {
        # Nothing to undo: the request ends with this process anyway.
        $null = $_
    }
}

function Receive-BotOutput {
    # Copies the bot's output to the log until the bot has exited (or -MaxSeconds have passed).
    # Reads with a timeout so that the loop also notices when the bot is gone while a stray child
    # process (a browser helper) still holds the output pipe open.
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [int]$MaxSeconds = 0
    )
    $reader = $Process.StandardOutput
    $started = Get-Date
    $quietAfterExit = 0
    while ($true) {
        if ($null -eq $script:PendingRead) { $script:PendingRead = $reader.ReadLineAsync() }
        $gotLine = $false
        try {
            $gotLine = $script:PendingRead.Wait(500)
        } catch {
            break
        }
        if ($gotLine) {
            $line = $script:PendingRead.Result
            $script:PendingRead = $null
            if ($null -eq $line) { break }
            $quietAfterExit = 0
            Write-BotLog $line
        } elseif ($Process.HasExited) {
            $quietAfterExit += 1
            if ($quietAfterExit -ge 6) { break }
        }
        if (-not $script:DashboardOpened -and -not $Background -and -not $NoBrowser) {
            if (Test-TcpPort -Port $script:DashboardPort) {
                $script:DashboardOpened = $true
                Start-Process -FilePath (Get-DashboardUrl) | Out-Null
            }
        }
        if ($MaxSeconds -gt 0 -and ((Get-Date) - $started).TotalSeconds -ge $MaxSeconds) { break }
    }
}

function Test-StopMarker {
    return [System.IO.File]::Exists($StopMarker)
}

function Invoke-BotOnce {
    # Prepares the database, runs "uv run cs2bot up" once and returns its exit code. Sets
    # $script:StopRequested when stop.ps1 asked for the stop.
    #
    # Ctrl+C (pressed in this window, or sent by stop.ps1) reaches the bot AND this script. The bot
    # shuts down by itself; PowerShell stops this script but still runs the "finally" block below,
    # which keeps copying the bot's last lines for up to 90 seconds and then ends whatever is still
    # alive, so that no half-stopped bot is left behind. Nothing in that block may write to the
    # pipeline (output of a script that is being stopped has nowhere to go), hence the .NET calls.
    param([int]$DockerTimeoutSeconds = 300)

    Start-DockerDesktop -TimeoutSeconds $DockerTimeoutSeconds
    Start-Postgres

    if (-not $Background) { Write-Step 'Applying database migrations (cs2bot db upgrade)' }
    $code = Invoke-Logged 'uv run cs2bot db upgrade'
    if ($code -ne 0) { throw "cs2bot db upgrade failed (exit code $code). See the lines above / the log file." }

    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'dashboard\dist\index.html'))) {
        Write-BotLog 'NOTE: the dashboard is not built (dashboard\dist is missing): the bot works, but the web page shows a notice. Run deploy\windows\install.ps1 or update.ps1 to build it.'
    }

    # cmd.exe merges the bot's error output into its normal output (2>&1), so one pipe carries both.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $env:ComSpec
    $startInfo.Arguments = '/d /c "uv run cs2bot up 2>&1"'
    $startInfo.WorkingDirectory = $RepoRoot
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.StandardOutputEncoding = $Utf8NoBom
    $startInfo.CreateNoWindow = $false

    if (Test-StopMarker) {
        # stop.ps1 ran while Docker / PostgreSQL were still starting: do not start the bot.
        [System.IO.File]::Delete($StopMarker)
        $script:StopRequested = $true
        Write-BotLogMarker 'stop requested before the bot started - not starting it'
        return 0
    }
    if (Test-BotRunning) {
        # Started by other means while we waited for Docker (e.g. "uv run cs2bot up" typed by hand).
        # Two bots would share one Betclic profile and one dashboard port: leave it running.
        $script:StopRequested = $true
        Write-BotLogMarker 'another cs2bot is already running - not starting a second one'
        return 0
    }
    Write-BotLogMarker ("cs2bot starting (start.ps1, dry_run={0}, background={1})" -f $script:DryRun, [bool]$Background)
    $script:PendingRead = $null
    $bot = [System.Diagnostics.Process]::Start($startInfo)
    $finishedNormally = $false
    $exitCode = 1
    try {
        Receive-BotOutput -Process $bot
        $finishedNormally = $true
    } finally {
        try {
            $script:DashboardOpened = $true
            [void]$bot.WaitForExit(2000)
            if (-not $bot.HasExited) { Receive-BotOutput -Process $bot -MaxSeconds 90 }
            if (-not $bot.HasExited) {
                Write-BotLog 'The bot did not stop within 90 seconds - ending it.'
                Stop-ProcessTree -ProcessId $bot.Id
                [void]$bot.WaitForExit(10000)
            }
            if ($bot.HasExited) { $exitCode = $bot.ExitCode }
            if (Test-StopMarker) {
                # stop.ps1 asked for this stop: report success so nothing restarts the bot.
                [System.IO.File]::Delete($StopMarker)
                $script:StopRequested = $true
                $exitCode = 0
            }
            Write-BotLogMarker "cs2bot stopped (exit code $exitCode)"
        } catch {
            $null = $_
        }
        if (-not $finishedNormally) {
            # Ctrl+C: PowerShell is unwinding and will not run any code after this block.
            if ($null -ne $script:LogWriter) { $script:LogWriter.Dispose(); $script:LogWriter = $null }
            if ($script:KeepAwake) { Disable-KeepAwake }
            Exit-StartLock
            if ($Background) {
                # Under Task Scheduler this PowerShell exists only for the bot: leave with the right
                # exit code (a plain "exit" further down would never be reached).
                [Environment]::Exit($exitCode)
            }
        }
    }
    return $exitCode
}

function Wait-BeforeRestart {
    # Sleeps up to $Seconds, but returns at once (with $false) when stop.ps1 asks for a stop.
    param([int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-StopMarker) {
            [System.IO.File]::Delete($StopMarker)
            return $false
        }
        Start-Sleep -Seconds 1
    }
    return $true
}

# ---------------------------------------------------------------------------------------------

$exitCode = 1
$script:KeepAwake = $false
$script:DryRun = $true
try {
    Assert-RepoReady
    foreach ($folder in @($LogDir, $RunDir)) { Initialize-RuntimeFolder -Path $folder }

    # Only one start.ps1 at a time (see Enter-StartLock). Whoever holds the lock is starting or
    # running the bot, or updating / restoring: leave it alone. No long wait, so that a bot
    # stopped on purpose is not started again by a start.ps1 that was waiting in the background.
    # Background mode waits a few seconds for a start.ps1 that is just finishing.
    $lockWait = 0
    if ($Background) { $lockWait = 15 }
    if (-not (Enter-StartLock -WaitSeconds $lockWait)) {
        if (-not $Background) {
            if (Test-BotRunning) {
                Write-Host "The bot is already running. Dashboard: $(Get-DashboardUrl)" -ForegroundColor Green
            } else {
                Write-Host ('Another start.ps1 is already starting the bot (for example the autostart task, which waits ' +
                    'for Docker after sign-in), or update / restore is running. Wait a few minutes and look at ' +
                    "$(Get-DashboardUrl), or check the newest file in $LogDir.") -ForegroundColor Yellow
            }
        }
        exit 0
    }

    if (Test-BotRunning) {
        if (-not $Background) {
            Write-Host "The bot is already running. Dashboard: $(Get-DashboardUrl)" -ForegroundColor Green
            Write-Host 'To stop it: deploy\windows\stop.cmd'
        }
        exit 0
    }
    # A stop request left over from an earlier run must not affect this one.
    Remove-Item -LiteralPath $StopMarker -Force -ErrorAction SilentlyContinue

    $supervise = ($Background -or $RestartOnCrash)
    $dockerTimeout = 300
    if ($Background) { $dockerTimeout = 900 }
    $script:DashboardPort = Get-EnvFileInt -Name 'CS2BOT_API__PORT' -Default 8000
    $script:DryRun = Test-DryRunEnabled

    if (-not $Background) {
        Write-Step 'Starting Docker and PostgreSQL'
        if (-not $script:DryRun) {
            Write-Host '    LIVE MODE: the dry run is off (CS2BOT_EXECUTOR__DRY_RUN) - the bot places REAL bets.' -ForegroundColor Yellow
        } else {
            Write-Host '    Dry run: bets are simulated, no real money is placed.' -ForegroundColor Green
        }
    }
    # The bot writes UTF-8 (PYTHONUTF8=1, team and player names); decode it as UTF-8, also in the
    # output of "cs2bot db upgrade" that Invoke-Logged reads through the console encoding.
    try { [Console]::OutputEncoding = $Utf8NoBom } catch { Write-Verbose 'Console encoding unchanged.' }

    if (-not $AllowSleep) {
        $script:KeepAwake = Enable-KeepAwake
        if (-not $Background) {
            if ($script:KeepAwake) {
                Write-Info 'Windows will not go to sleep on its own while the bot runs (the screen may still turn off).'
            } else {
                Write-Warn 'Could not ask Windows to stay awake. See deploy\windows\power-settings.ps1.'
            }
        }
    }
    if (-not $Background) {
        Write-Info "Dashboard: $(Get-DashboardUrl)"
        Write-Info "Log file:  $(Join-Path $LogDir 'cs2bot-<date>.log')"
        Write-Info 'Stop with Ctrl+C (or deploy\windows\stop.cmd from another window).'
    }

    $crashTimes = @()
    while ($true) {
        $runFailed = $false
        try {
            $exitCode = Invoke-BotOnce -DockerTimeoutSeconds $dockerTimeout
        } catch {
            # Docker / PostgreSQL / migrations failed before the bot started.
            if (-not $supervise) { throw }
            $runFailed = $true
            $exitCode = 1
            Write-BotLogMarker "start failed: $($_.Exception.Message)"
        }
        if ($script:StopRequested -or -not $supervise) { break }
        if (-not $runFailed -and $exitCode -eq 0) {
            Write-BotLog 'The bot exited normally (exit code 0) - not restarting it.'
            break
        }
        # A crash: restart with a growing pause, unless it keeps crashing.
        $hourAgo = (Get-Date).AddHours(-1)
        $crashTimes = @($crashTimes | Where-Object { $_ -ge $hourAgo })
        if ($crashTimes.Count -ge $MaxRestartsPerHour) {
            Write-BotLogMarker "giving up: $($crashTimes.Count) crash restarts within one hour. Fix the cause (see above), then start the bot again."
            break
        }
        $delay = [int][Math]::Min(600, 30 * [Math]::Pow(2, $crashTimes.Count))
        $crashTimes += (Get-Date)
        Write-BotLog "The bot stopped unexpectedly (exit code $exitCode). Restarting in $delay seconds (restart $($crashTimes.Count) of at most $MaxRestartsPerHour per hour)."
        if (-not (Wait-BeforeRestart -Seconds $delay)) {
            Write-BotLog 'Stop requested while waiting - not restarting.'
            $exitCode = 0
            break
        }
    }

    if ($null -ne $script:LogWriter) { $script:LogWriter.Dispose(); $script:LogWriter = $null }
    if ($script:KeepAwake) { Disable-KeepAwake }
    if (-not $Background) {
        if ($exitCode -eq 0) {
            Write-Host 'The bot has stopped.' -ForegroundColor Green
        } else {
            Write-Fail "The bot stopped with exit code $exitCode. See the newest file in $LogDir."
        }
    }
} catch {
    $message = $_.Exception.Message
    if (-not $Background) { Write-Fail $message }
    try {
        Initialize-RuntimeFolder -Path $LogDir
        $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
        $failLog = Join-Path $LogDir ('cs2bot-' + (Get-Date).ToString('yyyy-MM-dd') + '.log')
        if ($null -ne $script:LogWriter) { $script:LogWriter.Dispose(); $script:LogWriter = $null }
        [System.IO.File]::AppendAllText($failLog, "===== start.ps1 failed at ${stamp}: $message =====`r`n", $Utf8NoBom)
    } catch {
        Write-Verbose 'Could not write the failure to the log file.'
    }
    if ($script:KeepAwake) { Disable-KeepAwake }
    $exitCode = 1
} finally {
    # Also runs after Ctrl+C while Docker / PostgreSQL were still starting.
    Exit-StartLock
}
exit $exitCode
