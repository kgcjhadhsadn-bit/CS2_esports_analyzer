# Shared helpers for the cs2bot Windows scripts. Dot-source it; do not run it directly:
#
#     . (Join-Path $PSScriptRoot 'common.ps1')
#
# Written for Windows PowerShell 5.1 (the version that ships with Windows 10 / 11); it also runs in
# PowerShell 7. Rules followed by every script in this folder:
#
#   * Native programs (docker, uv, git, npm, winget) do not raise PowerShell errors when they fail.
#     Every call therefore goes through Invoke-Checked / Invoke-Quiet / Get-NativeOutput, which
#     look at $LASTEXITCODE.
#   * In PowerShell 5.1 a native program that writes to stderr while its stderr is redirected
#     ("2>&1", "2>$null") raises an error when $ErrorActionPreference is 'Stop'. Redirection is
#     therefore only used inside Invoke-Quiet / Get-NativeOutput, which switch to 'Continue'.
#   * Secrets from .env are never printed.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# $LASTEXITCODE does not exist until the first native program has run (strict mode would complain).
$global:LASTEXITCODE = 0

$script:RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$script:ComposeFile = Join-Path $script:RepoRoot 'docker-compose.yml'
$script:EnvFile = Join-Path $script:RepoRoot '.env'
$script:EnvExampleFile = Join-Path $script:RepoRoot '.env.example'
$script:LogDir = Join-Path $script:RepoRoot 'logs'
$script:BackupDir = Join-Path $script:RepoRoot 'backups'
$script:RunDir = Join-Path $script:RepoRoot 'data\run'
$script:StopMarker = Join-Path $script:RunDir 'stop.requested'
$script:PostgresContainer = 'cs2bot-postgres'
$script:PostgresVolume = 'cs2bot_pgdata'
$script:AutostartTaskName = 'cs2bot'
$script:WakeTaskName = 'cs2bot-wake'

# uv must use the committed lock file as it is and never re-resolve dependencies on the owner's PC.
$env:UV_FROZEN = '1'
# Python: UTF-8 output (team and player names) and no buffering when the output goes to a log file.
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'

# ---------------------------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------------------------

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Info {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "    $Message"
}

function Write-Ok {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "    OK: $Message" -ForegroundColor Green
}

function Write-Warn {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "    WARNING: $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ''
    Write-Host "ERROR: $Message" -ForegroundColor Red
}

function Confirm-Action {
    # Asks a yes/no question. Returns $true at once when -AssumeYes is given (unattended runs).
    param(
        [Parameter(Mandatory = $true)][string]$Question,
        [bool]$Default = $true,
        [switch]$AssumeYes
    )
    if ($AssumeYes) { return $true }
    $suffix = '[y/N]'
    if ($Default) { $suffix = '[Y/n]' }
    $answer = Read-Host "$Question $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return ($answer.Trim() -match '^(y|yes|t|tak)$')
}

# ---------------------------------------------------------------------------------------------
# Native programs
# ---------------------------------------------------------------------------------------------

function Test-CommandExists {
    param([Parameter(Mandatory = $true)][string]$Name)
    return ($null -ne (Get-Command -Name $Name -ErrorAction SilentlyContinue))
}

function Update-SessionPath {
    # A program installed a moment ago (winget, the uv installer) is not on this window's PATH yet.
    # Re-read PATH from the registry and add the usual install folders.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $extra = @(
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:USERPROFILE '.cargo\bin'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'),
        (Join-Path $env:ProgramFiles 'Git\cmd'),
        (Join-Path $env:ProgramFiles 'nodejs')
    )
    $seen = @{}
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($entry in (@($env:Path, $machine, $user) -join ';').Split(';') + $extra) {
        if ([string]::IsNullOrWhiteSpace($entry)) { continue }
        $key = $entry.Trim().TrimEnd('\').ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $parts.Add($entry.Trim())
    }
    $env:Path = ($parts.ToArray() -join ';')
}

function Resolve-NativeCommand {
    # Full path of a program on PATH ("docker" -> "C:\Program Files\Docker\...\docker.exe").
    # Start-Process and .NET process APIs are given full paths so they never pick a wrong program.
    param([Parameter(Mandatory = $true)][string]$Name)
    if (Test-Path -LiteralPath $Name -PathType Leaf) { return (Resolve-Path -LiteralPath $Name).Path }
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) { throw "'$Name' was not found on PATH." }
    return $command.Source
}

function Invoke-Checked {
    # Runs a native program with its output shown, and stops the script if it fails.
    #   Invoke-Checked 'uv sync' 'uv' @('sync', '--frozen')
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    # Out-Host: the program's output is shown but never becomes the return value of the caller.
    & $FilePath @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (exit code $LASTEXITCODE)."
    }
}

function Invoke-Quiet {
    # Runs a native program, hides everything it prints and returns its exit code.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @Arguments 2>&1 | Out-Null
        return $LASTEXITCODE
    } catch {
        return 1
    }
}

function Get-NativeOutput {
    # Runs a native program and returns its standard output as one trimmed string. Error output is
    # dropped. Returns $null when the program fails or cannot be started.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $FilePath @Arguments 2>$null)
        if ($LASTEXITCODE -ne 0) { return $null }
        return (($lines | ForEach-Object { "$_" }) -join "`n").Trim()
    } catch {
        return $null
    }
}

function Invoke-ProcessToFile {
    # Runs a native program with its standard output written straight into a file. This is the only
    # safe way to capture BINARY output (a pg_dump archive) in PowerShell 5.1: ">" and pipelines
    # re-encode the data as text. Returns the exit code; error output goes to $ErrorFile.
    # Arguments are joined with spaces as they are: quote any argument that contains a space.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$OutputFile,
        [Parameter(Mandatory = $true)][string]$ErrorFile,
        [string]$InputFile = ''
    )
    foreach ($argument in $Arguments) {
        if ($argument -match '\s') { throw "Invoke-ProcessToFile: argument '$argument' contains a space." }
    }
    $startArgs = @{
        FilePath               = (Resolve-NativeCommand -Name $FilePath)
        ArgumentList           = $Arguments
        RedirectStandardOutput = $OutputFile
        RedirectStandardError  = $ErrorFile
        NoNewWindow            = $true
        Wait                   = $true
        PassThru               = $true
    }
    if ($InputFile -ne '') { $startArgs['RedirectStandardInput'] = $InputFile }
    $process = Start-Process @startArgs
    return $process.ExitCode
}

function Get-FileTail {
    # Last lines of a text file as one string ('' when the file is missing or empty).
    param([Parameter(Mandatory = $true)][string]$Path, [int]$Lines = 15)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $tail = @(Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction SilentlyContinue)
    return ($tail -join "`n")
}

# ---------------------------------------------------------------------------------------------
# Files and settings
# ---------------------------------------------------------------------------------------------

function Initialize-RuntimeFolder {
    # Creates a folder for logs / backups and keeps git from ever offering its contents for commit
    # (database dumps and logs must never end up in the repository).
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
    $ignoreFile = Join-Path $Path '.gitignore'
    if (-not (Test-Path -LiteralPath $ignoreFile)) {
        [System.IO.File]::WriteAllText($ignoreFile, "*`n", (New-Object System.Text.UTF8Encoding($false)))
    }
}

function Get-EnvFileValue {
    # Reads one KEY=value line from .env ('' when missing). Use only for non-secret settings, or to
    # test whether a secret is filled in - never print the result.
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$Default = '',
        [string]$Path = $script:EnvFile
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $Default }
    $pattern = '^\s*(?:export\s+)?' + [regex]::Escape($Name) + '\s*=(.*)$'
    $value = $null
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match $pattern) { $value = $Matches[1].Trim() }
    }
    if ($null -eq $value) { return $Default }
    if ($value.Length -ge 2) {
        $first = $value.Substring(0, 1)
        $last = $value.Substring($value.Length - 1, 1)
        if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
            $value = $value.Substring(1, $value.Length - 2)
        }
    }
    if ($value -eq '') { return $Default }
    return $value
}

function Get-DashboardUrl {
    $apiHost = Get-EnvFileValue -Name 'CS2BOT_API__HOST' -Default '127.0.0.1'
    if ($apiHost -eq '0.0.0.0' -or $apiHost -eq '::') { $apiHost = '127.0.0.1' }
    $apiPort = Get-EnvFileValue -Name 'CS2BOT_API__PORT' -Default '8000'
    return "http://${apiHost}:${apiPort}"
}

function Test-TcpPort {
    # True when something accepts TCP connections on the port (quick: gives up after 400 ms).
    param([string]$HostName = '127.0.0.1', [Parameter(Mandatory = $true)][int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($pending)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-DashboardAlive {
    # True when the bot's API answers its liveness probe (GET /api/ping needs no login).
    try {
        $response = Invoke-RestMethod -Uri ((Get-DashboardUrl) + '/api/ping') -TimeoutSec 3
        return ($response.status -eq 'ok')
    } catch {
        return $false
    }
}

function Assert-RepoReady {
    # Common pre-flight for scripts that need an installed bot.
    Update-SessionPath
    if (-not (Test-Path -LiteralPath $script:EnvFile)) {
        throw "No .env file found in $($script:RepoRoot). Run deploy\windows\install.ps1 first."
    }
    foreach ($tool in @('uv', 'docker')) {
        if (-not (Test-CommandExists $tool)) {
            throw "'$tool' was not found. Run deploy\windows\install.ps1 first (or open a new PowerShell window if you have just installed it)."
        }
    }
    Set-Location -LiteralPath $script:RepoRoot
}

# ---------------------------------------------------------------------------------------------
# Docker and PostgreSQL
# ---------------------------------------------------------------------------------------------

function Test-DockerRunning {
    return ((Invoke-Quiet 'docker' @('version', '--format', '{{.Server.Version}}')) -eq 0)
}

function Start-DockerDesktop {
    # Makes sure the Docker engine answers; starts Docker Desktop and waits when it does not.
    param([int]$TimeoutSeconds = 300)
    if (Test-DockerRunning) { return }
    $exe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        throw 'Docker Desktop is not installed (or not in its usual folder). Run deploy\windows\install.ps1, or start Docker yourself and try again.'
    }
    $alreadyOpen = @(Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)
    if ($alreadyOpen.Count -eq 0) {
        Write-Info 'Starting Docker Desktop...'
        Start-Process -FilePath $exe | Out-Null
    } else {
        Write-Info 'Docker Desktop is open but its engine is not ready yet. Waiting...'
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if (Test-DockerRunning) {
            Write-Ok 'Docker engine is running.'
            return
        }
    }
    throw "Docker did not become ready within $TimeoutSeconds seconds. Open Docker Desktop, accept its terms if it asks, wait for 'Engine running', then run this script again."
}

function Test-PostgresContainerRunning {
    # "docker inspect" by exact name (a "docker ps --filter name=" filter is a regular expression).
    $running = Get-NativeOutput 'docker' @('inspect', '--format', '{{.State.Running}}', $script:PostgresContainer)
    return ($running -eq 'true')
}

function Get-PostgresHealth {
    # 'healthy', 'starting', 'unhealthy', or '' when the container does not exist.
    $status = Get-NativeOutput 'docker' @('inspect', '--format', '{{.State.Health.Status}}', $script:PostgresContainer)
    if ($null -eq $status) { return '' }
    return $status
}

function Start-Postgres {
    # docker compose up -d, then wait until the health check passes. Safe to call when it already runs.
    param([int]$TimeoutSeconds = 180)
    if (-not (Test-PostgresContainerRunning)) {
        $port = [int](Get-EnvFileValue -Name 'POSTGRES_PORT' -Default '5432')
        if (Test-TcpPort -Port $port) {
            throw ("Port $port on this PC is already used by another program (often a PostgreSQL that was installed separately). " +
                "Stop that program, or choose another port: put POSTGRES_PORT=5433 in .env AND change the port in CS2BOT_DATABASE__URL to 5433. " +
                'See "Port 5432 is in use" in docs\WINDOWS_SETUP.md.')
        }
    }
    Invoke-Checked 'Starting PostgreSQL (docker compose up)' 'docker' @('compose', '-f', $script:ComposeFile, 'up', '-d')
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $health = ''
    while ((Get-Date) -lt $deadline) {
        $health = Get-PostgresHealth
        if ($health -eq 'healthy') {
            Write-Ok 'PostgreSQL is healthy.'
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "PostgreSQL did not become healthy within $TimeoutSeconds seconds (state: '$health'). Look at: docker compose logs postgres"
}

function Get-PostgresIdentity {
    # User and database name the container was (or will be) created with.
    return @{
        User     = (Get-EnvFileValue -Name 'POSTGRES_USER' -Default 'cs2bot')
        Database = (Get-EnvFileValue -Name 'POSTGRES_DB' -Default 'cs2bot')
    }
}

# ---------------------------------------------------------------------------------------------
# The bot process
# ---------------------------------------------------------------------------------------------

function Get-BotProcess {
    # Every process that belongs to a running "cs2bot up": the cmd.exe wrapper started by
    # start.ps1, uv.exe, the cs2bot.exe launcher and python.exe. Found by command line, so no PID
    # file can go stale. Call it as @(Get-BotProcess).
    $names = @('python.exe', 'pythonw.exe', 'cs2bot.exe', 'uv.exe', 'cmd.exe')
    # Matches "uv run cs2bot up", "...\cs2bot.exe" up, and "python -m cs2bot.cli up".
    $pattern = 'cs2bot(\.exe|\.cli)?["'']?\s+up(\s|"|$)'
    $found = @()
    $all = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue)
    foreach ($process in $all) {
        if ($names -notcontains ([string]$process.Name).ToLowerInvariant()) { continue }
        if ($process.ProcessId -eq $PID) { continue }
        $commandLine = [string]$process.CommandLine
        if ([string]::IsNullOrEmpty($commandLine)) { continue }
        if ($commandLine -match $pattern) { $found += $process }
    }
    return $found
}

function Test-BotRunning {
    return (@(Get-BotProcess).Count -gt 0)
}

function Stop-ProcessTree {
    # Ends a process and everything it started (taskkill /T /F). Uses .NET calls only, so it also
    # works inside a "finally" block while PowerShell is stopping the script after Ctrl+C.
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    try {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        $info.Arguments = "/PID $ProcessId /T /F"
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $killer = [System.Diagnostics.Process]::Start($info)
        [void]$killer.WaitForExit(15000)
    } catch {
        Write-Verbose "taskkill for process $ProcessId failed: $($_.Exception.Message)"
    }
}

function Get-AutostartTask {
    # The Task Scheduler entry created by register-autostart.ps1, or $null.
    return (Get-ScheduledTask -TaskName $script:AutostartTaskName -ErrorAction SilentlyContinue)
}

function Build-Dashboard {
    # npm ci + npm run build in dashboard/ (skipped with a note when the dashboard is not there).
    $dashboardDir = Join-Path $script:RepoRoot 'dashboard'
    if (-not (Test-Path -LiteralPath (Join-Path $dashboardDir 'package.json'))) {
        Write-Info 'No dashboard/ folder in this checkout yet - skipping the dashboard build.'
        return
    }
    if (-not (Test-CommandExists 'npm.cmd')) {
        throw 'Node.js (npm) was not found. Install "Node.js LTS" (winget install OpenJS.NodeJS.LTS), open a new PowerShell window and run this script again.'
    }
    Push-Location -LiteralPath $dashboardDir
    try {
        # npm.cmd on purpose: plain "npm" resolves to npm.ps1, which a restricted execution policy blocks.
        Invoke-Checked 'Installing dashboard packages (npm ci)' 'npm.cmd' @('ci', '--no-audit', '--no-fund')
        Invoke-Checked 'Building the dashboard (npm run build)' 'npm.cmd' @('run', 'build')
    } finally {
        Pop-Location
    }
    Write-Ok 'Dashboard built (dashboard\dist).'
}

# ---------------------------------------------------------------------------------------------
# Task Scheduler and windows
# ---------------------------------------------------------------------------------------------

function Get-CurrentUserId {
    # DOMAIN\user (or PC-NAME\user) of the person running this script.
    return [Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function Get-WindowsPowerShellPath {
    # Windows PowerShell 5.1 is on every Windows 10 / 11 PC; the scheduled tasks always use it.
    return (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
}

function New-ScriptTaskAction {
    # A Task Scheduler action that runs one of these scripts.
    #
    # Hidden: the script runs inside an explicitly started classic console host (conhost.exe) with
    # -WindowStyle Hidden. On Windows 11 with "Windows Terminal" as the default terminal,
    # powershell.exe -WindowStyle Hidden alone leaves a terminal window open for as long as the bot
    # runs (microsoft/terminal issue 12464); starting conhost.exe ourselves avoids the hand-over to
    # Windows Terminal, so the window only flashes for a moment at logon. The console still exists
    # (invisibly), which stop.ps1 needs to send Ctrl+C to the bot.
    param(
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [string]$ScriptArguments = '',
        [switch]$Visible
    )
    $scriptPath = Join-Path $PSScriptRoot $ScriptName
    $powershell = Get-WindowsPowerShellPath
    $style = 'Hidden'
    if ($Visible) { $style = 'Minimized' }
    $psArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle $style -File `"$scriptPath`""
    if ($ScriptArguments -ne '') { $psArguments = "$psArguments $ScriptArguments" }
    $conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
    if (Test-Path -LiteralPath $conhost) {
        return (New-ScheduledTaskAction -Execute $conhost -Argument "`"$powershell`" $psArguments" -WorkingDirectory $script:RepoRoot)
    }
    return (New-ScheduledTaskAction -Execute $powershell -Argument $psArguments -WorkingDirectory $script:RepoRoot)
}

function Register-UserTask {
    # Registers (or replaces) a Task Scheduler task that runs as the current user, only while that
    # user is signed in (needed: the bot shows a browser window on the desktop). Explains
    # "Access is denied" instead of failing with a cryptic message.
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)]$Action,
        [Parameter(Mandatory = $true)]$Trigger,
        [Parameter(Mandatory = $true)]$Settings
    )
    $principal = New-ScheduledTaskPrincipal -UserId (Get-CurrentUserId) -LogonType Interactive -RunLevel Limited
    try {
        Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Description $Description -Action $Action `
            -Trigger $Trigger -Principal $principal -Settings $Settings -Force | Out-Null
    } catch {
        $message = $_.Exception.Message
        if ($message -match 'denied|0x80070005') {
            throw ("Windows refused to create the scheduled task '$TaskName' (access denied). Open PowerShell with " +
                '"Run as administrator" - signed in as the same Windows user that runs the bot - and run this script again.')
        }
        throw "Could not create the scheduled task '$TaskName': $message"
    }
}

function Start-ScriptInNewWindow {
    # Opens a new PowerShell window that runs one of these scripts (used to restart the bot).
    param(
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [string]$ScriptArguments = ''
    )
    $scriptPath = Join-Path $PSScriptRoot $ScriptName
    $psArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
    if ($ScriptArguments -ne '') { $psArguments = "$psArguments $ScriptArguments" }
    Start-Process -FilePath (Get-WindowsPowerShellPath) -ArgumentList $psArguments -WorkingDirectory $script:RepoRoot | Out-Null
}
