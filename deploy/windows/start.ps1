<#
.SYNOPSIS
    Starts the bot: Docker Desktop -> PostgreSQL -> "uv run cs2bot up" (dashboard + scheduler).

.DESCRIPTION
    1. Does nothing (exit 0) when the bot is already running.
    2. Starts Docker Desktop if needed, starts PostgreSQL and waits until it is healthy.
    3. Applies pending database migrations (cs2bot db upgrade; a no-op when there are none).
    4. Runs "uv run cs2bot up" and copies everything it prints to logs\cs2bot-YYYY-MM-DD.log
       (one file per day, files older than -LogRetentionDays are deleted).
    5. While the bot runs, Windows is asked not to go to sleep on its own (the screen may still
       turn off). The request ends when the bot stops; no power setting is changed. Use -AllowSleep
       to switch this off.

    Stop the bot with Ctrl+C in this window, or with stop.ps1 from another window.
    The window can be minimised. Closing it stops the bot.

.PARAMETER Background
    For the Task Scheduler entry (register-autostart.ps1): no console output, no browser tab,
    waits longer for Docker after logon.

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

# ---------------------------------------------------------------------------------------------

$exitCode = 1
try {
    Assert-RepoReady
    foreach ($folder in @($LogDir, $RunDir)) { Initialize-RuntimeFolder -Path $folder }

    if (Test-BotRunning) {
        if (-not $Background) {
            Write-Host "The bot is already running. Dashboard: $(Get-DashboardUrl)" -ForegroundColor Green
            Write-Host 'To stop it: deploy\windows\stop.cmd'
        }
        exit 0
    }
    # A stop request left over from an earlier run must not affect this one.
    Remove-Item -LiteralPath $StopMarker -Force -ErrorAction SilentlyContinue

    if (-not $Background) { Write-Step 'Starting Docker and PostgreSQL' }
    $dockerTimeout = 300
    if ($Background) { $dockerTimeout = 900 }
    Start-DockerDesktop -TimeoutSeconds $dockerTimeout
    Start-Postgres

    if (-not $Background) { Write-Step 'Applying database migrations (cs2bot db upgrade)' }
    Invoke-Checked 'cs2bot db upgrade' 'uv' @('run', 'cs2bot', 'db', 'upgrade')

    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'dashboard\dist\index.html'))) {
        Write-Warn 'The dashboard is not built (dashboard\dist is missing): the bot works, but the web page shows a notice. Run deploy\windows\install.ps1 to build it.'
    }
    $dryRun = (Get-EnvFileValue -Name 'CS2BOT_EXECUTOR__DRY_RUN' -Default 'true').ToLowerInvariant()
    if (-not $Background) {
        if ($dryRun -eq 'false') {
            Write-Host '    LIVE MODE: CS2BOT_EXECUTOR__DRY_RUN=false - the bot places REAL bets.' -ForegroundColor Yellow
        } else {
            Write-Host '    Dry run: bets are simulated, no real money is placed.' -ForegroundColor Green
        }
    }

    $keepAwake = $false
    if (-not $AllowSleep) {
        $keepAwake = Enable-KeepAwake
        if (-not $Background) {
            if ($keepAwake) {
                Write-Info 'Windows will not go to sleep on its own while the bot runs (the screen may still turn off).'
            } else {
                Write-Warn 'Could not ask Windows to stay awake. See deploy\windows\power-settings.ps1.'
            }
        }
    }

    $script:DashboardPort = [int](Get-EnvFileValue -Name 'CS2BOT_API__PORT' -Default '8000')
    $script:DashboardOpened = $false
    $script:PendingRead = $null

    if (-not $Background) {
        Write-Step 'Starting the bot (uv run cs2bot up)'
        Write-Info "Dashboard: $(Get-DashboardUrl)"
        Write-Info "Log file:  $(Join-Path $LogDir 'cs2bot-<date>.log')"
        Write-Info 'Stop with Ctrl+C (or deploy\windows\stop.cmd from another window).'
        Write-Host ''
        # Make the console decode the bot's UTF-8 output correctly.
        try { [Console]::OutputEncoding = $Utf8NoBom } catch { Write-Verbose 'Console encoding unchanged.' }
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

    Write-BotLog ("===== cs2bot starting at {0} (start.ps1, dry_run={1}) =====" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'), $dryRun)
    $bot = [System.Diagnostics.Process]::Start($startInfo)
    try {
        Receive-BotOutput -Process $bot
    } finally {
        # Reached on a normal exit AND after Ctrl+C (pressed here, or sent by stop.ps1). After
        # Ctrl+C the bot has received the same signal and is shutting down by itself: keep copying
        # its last lines for up to 90 seconds, then end whatever is still alive so that no
        # half-stopped bot is left behind. Nothing in this block may write to the pipeline (output
        # of a script that is being stopped has nowhere to go), hence the .NET calls.
        $script:DashboardOpened = $true
        [void]$bot.WaitForExit(2000)
        if (-not $bot.HasExited) { Receive-BotOutput -Process $bot -MaxSeconds 90 }
        if (-not $bot.HasExited) {
            Stop-ProcessTree -ProcessId $bot.Id
            [void]$bot.WaitForExit(10000)
        }
        if ($bot.HasExited) { $exitCode = $bot.ExitCode }
        if ([System.IO.File]::Exists($StopMarker)) {
            # stop.ps1 asked for this stop: report success so Task Scheduler does not restart the bot.
            [System.IO.File]::Delete($StopMarker)
            $exitCode = 0
        }
        $script:LogWriter.WriteLine(("===== cs2bot stopped at {0} (exit code {1}) =====" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'), $exitCode))
        $script:LogWriter.Dispose()
        $script:LogWriter = $null
        if ($keepAwake) { Disable-KeepAwake }
        if ($Background) {
            # Under Task Scheduler this PowerShell exists only for the bot. Leave with the right
            # exit code even when PowerShell is unwinding after Ctrl+C (a plain "exit" further
            # down would never be reached in that case).
            [Environment]::Exit($exitCode)
        }
    }

    if (-not $Background) {
        if ($exitCode -eq 0) {
            Write-Host 'The bot has stopped.' -ForegroundColor Green
        } else {
            Write-Fail "The bot stopped with exit code $exitCode. See the newest file in $LogDir."
        }
    }
} catch {
    $message = $_.Exception.Message
    Write-Fail $message
    try {
        Initialize-RuntimeFolder -Path $LogDir
        $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
        $failLog = Join-Path $LogDir ('cs2bot-' + (Get-Date).ToString('yyyy-MM-dd') + '.log')
        [System.IO.File]::AppendAllText($failLog, "===== start.ps1 failed at ${stamp}: $message =====`r`n", $Utf8NoBom)
    } catch {
        Write-Verbose 'Could not write the failure to the log file.'
    }
    $exitCode = 1
}
exit $exitCode
