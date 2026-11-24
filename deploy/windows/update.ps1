<#
.SYNOPSIS
    Updates cs2bot to the newest version: git pull, Python packages, browser, database tables,
    dashboard. Takes a database backup first.

.DESCRIPTION
    1. Stops the bot if it is running (it is started again at the end).
    2. If you edited files that belong to the repository (for example
       config\betclic_selectors.yaml), they are put aside with "git stash" and re-applied after the
       update. Your .env, logs, backups and data\ are never touched.
    3. Backs up the database (backup-db.ps1 -Label before-update), unless -NoBackup.
    4. git pull --ff-only (only fast-forward updates; nothing is merged or overwritten).
    5. uv sync, playwright install chromium, docker compose up -d (a new PostgreSQL image is
       pulled only when docker-compose.yml names a new one), cs2bot db upgrade, dashboard build.
    6. Starts the bot again if it was running before (through the autostart task when there is
       one, otherwise in a new window), unless -NoRestart.

.PARAMETER Yes
    Do not ask questions (stop the bot, stash local edits).

.PARAMETER NoBackup
    Skip the database backup.

.PARAMETER NoRestart
    Do not start the bot again at the end.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\update.ps1
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$NoBackup,
    [switch]$NoRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

function Restart-Bot {
    $task = Get-AutostartTask
    if ($null -ne $task) {
        Start-ScheduledTask -TaskName $AutostartTaskName
        Write-Ok "Bot started in the background (Task Scheduler entry '$AutostartTaskName'). Dashboard: $(Get-DashboardUrl)"
    } else {
        Start-ScriptInNewWindow -ScriptName 'start.ps1'
        Write-Ok 'Bot started in a new window.'
    }
}

# ---------------------------------------------------------------------------------------------

$stashed = $false
try {
    Assert-RepoReady
    if (-not (Test-CommandExists 'git')) { throw 'Git was not found. Install it (winget install Git.Git) or run install.ps1.' }
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot '.git'))) {
        throw "$RepoRoot is not a git checkout, so it cannot be updated with git pull. Download the new version instead."
    }

    Write-Step 'Checking the bot'
    $wasRunning = Test-BotRunning
    if ($wasRunning) {
        if (-not (Confirm-Action 'The bot is running and must be stopped for the update. Stop it now?' -AssumeYes:$Yes)) {
            throw 'Update cancelled - the bot keeps running.'
        }
        & (Join-Path $PSScriptRoot 'stop.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'The bot could not be stopped. Nothing was updated.' }
    } else {
        Write-Ok 'The bot is not running.'
    }
    # Hold the start lock until the update is finished, so that the autostart / wake task cannot
    # start the bot on half-updated files. A start.ps1 that is still waiting for Docker holds it.
    if (-not (Enter-StartLock -WaitSeconds 60)) {
        throw ('start.ps1 is still starting the bot (for example the autostart task, which waits for Docker after ' +
            'sign-in). Wait until the dashboard answers, or run stop.cmd, then run update again. Nothing was updated.')
    }

    Write-Step 'Checking for local changes'
    $branch = Get-NativeOutput 'git' @('rev-parse', '--abbrev-ref', 'HEAD')
    $before = Get-NativeOutput 'git' @('rev-parse', 'HEAD')
    if ($null -eq $before) { throw 'git could not read this checkout (git rev-parse HEAD failed).' }
    if ($branch -eq 'HEAD') { throw 'This checkout is not on a branch (detached HEAD). Run "git checkout main" first.' }
    $changes = Get-NativeOutput 'git' @('status', '--porcelain', '--untracked-files=no')
    if (-not [string]::IsNullOrWhiteSpace($changes)) {
        Write-Warn 'These repository files were changed on this PC:'
        foreach ($line in $changes.Split("`n")) { Write-Info "  $line" }
        if (-not (Confirm-Action 'Put them aside (git stash) and re-apply them after the update?' -AssumeYes:$Yes)) {
            throw 'Update cancelled. Commit or undo your changes first (git status shows them).'
        }
        Invoke-Checked 'git stash' 'git' @('stash', 'push', '--message', ('cs2bot update ' + (Get-Date).ToString('yyyy-MM-dd HH:mm')))
        $stashed = $true
        Write-Ok 'Local changes put aside.'
    } else {
        Write-Ok 'No local changes.'
    }

    if (-not $NoBackup) {
        Write-Step 'Backing up the database first'
        & (Join-Path $PSScriptRoot 'backup-db.ps1') -Label 'before-update'
        if ($LASTEXITCODE -ne 0) { throw 'The backup failed, so nothing was updated. Fix it, or run update.ps1 -NoBackup.' }
    }

    Write-Step "Downloading the newest version (git pull, branch $branch)"
    Invoke-Checked 'git pull' 'git' @('pull', '--ff-only')
    $after = Get-NativeOutput 'git' @('rev-parse', 'HEAD')

    if ($stashed) {
        Write-Step 'Re-applying your local changes (git stash pop)'
        & git stash pop
        if ($LASTEXITCODE -ne 0) {
            $stashed = $false
            throw ('Your local changes conflict with the update. They are kept in "git stash list"; the new version is ' +
                'installed in the files git did not complain about. Resolve the conflicts shown by "git status" ' +
                '(or ask for help), then run update.ps1 again.')
        }
        $stashed = $false
        Write-Ok 'Local changes re-applied.'
    }

    Write-Step 'Updating the Python packages (uv sync)'
    Invoke-Checked 'uv sync' 'uv' @('sync', '--frozen')

    Write-Step 'Updating the bot''s Chromium (playwright install chromium)'
    Invoke-Checked 'playwright install chromium' 'uv' @('run', 'playwright', 'install', 'chromium')

    Write-Step 'Starting PostgreSQL'
    Start-DockerDesktop
    Start-Postgres

    Write-Step 'Updating the database tables (cs2bot db upgrade)'
    Invoke-Checked 'cs2bot db upgrade' 'uv' @('run', 'cs2bot', 'db', 'upgrade')

    Write-Step 'Building the dashboard'
    Build-Dashboard

    Write-Host ''
    if ($before -eq $after) {
        Write-Host 'Already up to date - no new version was available.' -ForegroundColor Green
    } else {
        Write-Host 'Updated. Changes:' -ForegroundColor Green
        & git --no-pager log --oneline --no-decorate "$before..$after"
        Write-Host 'Read docs\OPERATIONS.md if the update mentions new settings (compare .env with .env.example).'
    }

    Exit-StartLock
    if ($wasRunning -and -not $NoRestart) {
        Write-Step 'Starting the bot again'
        Restart-Bot
    }
    exit 0
} catch {
    Write-Fail $_.Exception.Message
    if ($stashed) {
        & git stash pop
        if ($LASTEXITCODE -eq 0) {
            Write-Info 'Your local changes were re-applied.'
        } else {
            Write-Warn 'Your local changes are still in "git stash list" (re-apply them with: git stash pop).'
        }
    }
    exit 1
} finally {
    Exit-StartLock
}
