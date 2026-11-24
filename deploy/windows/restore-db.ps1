<#
.SYNOPSIS
    Replaces the cs2bot database with a backup made by backup-db.ps1. Asks for confirmation.

.DESCRIPTION
    1. Refuses to run while the bot is running (stop it first: stop.cmd).
    2. Shows the chosen backup (default: the newest file in backups\) and asks you to type RESTORE.
    3. Makes a safety backup of the CURRENT database first ("...-before-restore.dump"), unless
       -NoSafetyBackup.
    4. Drops the database, creates it empty and restores the backup into it (pg_restore inside the
       PostgreSQL container).
    5. Runs "cs2bot db upgrade" so a backup from an older version gets the current tables.

    Everything in the database goes back to the moment of the backup: bets, ledger, odds history,
    the kill switch / pause state and the thresholds changed on the dashboard. Bets placed on
    Betclic after that moment are NOT in the restored database - check "Moje zaklady" on Betclic.

.PARAMETER BackupFile
    The .dump file to restore (default: the newest backups\cs2bot-*.dump).

.PARAMETER List
    Only list the available backups.

.PARAMETER NoSafetyBackup
    Do not back up the current database first.

.PARAMETER Force
    Do not ask for confirmation (for scripted use only).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\restore-db.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\restore-db.ps1 -BackupFile backups\cs2bot-20261120-043000.dump
#>
[CmdletBinding()]
param(
    [string]$BackupFile = '',
    [switch]$List,
    [switch]$NoSafetyBackup,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$ContainerDumpPath = '/tmp/cs2bot-restore.dump'

function Get-Backups {
    return @(Get-ChildItem -LiteralPath $BackupDir -Filter 'cs2bot-*.dump' -File -ErrorAction SilentlyContinue |
        Sort-Object -Property LastWriteTime -Descending)
}

function Show-Backups {
    $backups = Get-Backups
    if ($backups.Count -eq 0) {
        Write-Info "No backups in $BackupDir."
        return
    }
    Write-Info "Backups in ${BackupDir} (newest first):"
    foreach ($file in ($backups | Select-Object -First 20)) {
        Write-Info ('  {0}   {1}   {2:N1} MB' -f $file.LastWriteTime.ToString('yyyy-MM-dd HH:mm'), $file.Name, ($file.Length / 1MB))
    }
}

function Resolve-BackupFile {
    if ($BackupFile -eq '') {
        $backups = Get-Backups
        if ($backups.Count -eq 0) { throw "No backups found in $BackupDir. Make one with backup-db.ps1, or pass -BackupFile." }
        return $backups[0].FullName
    }
    $candidate = $BackupFile
    if (-not [System.IO.Path]::IsPathRooted($candidate)) {
        # Relative paths: from the current folder first, then from the repository.
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $RepoRoot $BackupFile }
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "Backup file not found: $BackupFile" }
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Invoke-InContainer {
    # docker exec <container> <args>; throws with the error output when it fails.
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $errorFile = Join-Path $RunDir 'restore.err'
    $outputFile = Join-Path $RunDir 'restore.out'
    $code = Invoke-ProcessToFile -FilePath 'docker' -Arguments (@('exec', $PostgresContainer) + $Arguments) `
        -OutputFile $outputFile -ErrorFile $errorFile
    if ($code -ne 0) {
        throw "$Description failed (exit code $code): $(Get-FileTail -Path $errorFile -Lines 25)"
    }
    Remove-Item -LiteralPath $outputFile, $errorFile -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------------------------

try {
    Assert-RepoReady
    foreach ($folder in @($BackupDir, $RunDir)) { Initialize-RuntimeFolder -Path $folder }
    if ($List) {
        Show-Backups
        exit 0
    }
    # Hold the start lock for the whole restore: an autostart / wake task (or start.cmd) that fires
    # now waits instead of starting the bot on a half-restored database.
    if (-not (Enter-StartLock)) {
        throw ('start.ps1 (perhaps the autostart task waiting for Docker), update.ps1 or another restore is ' +
            'running. Stop the bot first (deploy\windows\stop.cmd), wait a minute, then run this script again.')
    }
    if (Test-BotRunning) {
        throw 'The bot is running. Stop it first (deploy\windows\stop.cmd), then run this script again.'
    }

    $file = Resolve-BackupFile
    $info = Get-Item -LiteralPath $file
    $identity = Get-PostgresIdentity

    Write-Step 'Restore the database from a backup'
    Write-Info "Backup:   $($info.FullName)"
    Write-Info ('Made:     {0}   Size: {1:N1} MB' -f $info.LastWriteTime.ToString('yyyy-MM-dd HH:mm'), ($info.Length / 1MB))
    Write-Info "Database: $($identity.Database) in the Docker container $PostgresContainer"
    Write-Host ''
    Write-Host '    Everything in the database will be replaced by the content of this backup.' -ForegroundColor Yellow
    Write-Host '    Bets placed on Betclic after the backup was made will be missing from it.' -ForegroundColor Yellow
    if (-not $Force) {
        $answer = Read-Host '    Type RESTORE (capital letters) to continue, anything else to cancel'
        if ($answer -cne 'RESTORE') {
            Write-Host 'Cancelled - nothing was changed.' -ForegroundColor Green
            exit 0
        }
    }

    Write-Step 'Making sure PostgreSQL is running'
    Start-DockerDesktop
    Start-Postgres

    if (-not $NoSafetyBackup) {
        Write-Step 'Safety backup of the current database'
        & (Join-Path $PSScriptRoot 'backup-db.ps1') -Label 'before-restore' -NoPrune
        if ($LASTEXITCODE -ne 0) {
            throw 'The safety backup failed, so nothing was restored. Fix the problem, or run again with -NoSafetyBackup if the current database is broken anyway.'
        }
    }

    Write-Step 'Copying the backup into the PostgreSQL container'
    Invoke-Checked 'docker cp' 'docker' @('cp', $info.FullName, ($PostgresContainer + ':' + $ContainerDumpPath))

    try {
        Write-Step 'Re-creating the database'
        # --force ends any open connection (PostgreSQL 13+). dropdb/createdb connect to the
        # "postgres" maintenance database.
        Invoke-InContainer 'dropdb' @('dropdb', '--username', $identity.User, '--if-exists', '--force', $identity.Database)
        Invoke-InContainer 'createdb' @('createdb', '--username', $identity.User, '--owner', $identity.User, $identity.Database)

        Write-Step 'Restoring the backup (pg_restore) - large databases take several minutes'
        Invoke-InContainer 'pg_restore' @('pg_restore', '--username', $identity.User, '--dbname', $identity.Database,
            '--no-owner', '--exit-on-error', $ContainerDumpPath)
        Write-Ok 'Backup restored.'
    } catch {
        Write-Fail $_.Exception.Message
        throw ('The restore did not finish; the database may be incomplete. Run this script again, or restore the ' +
            'safety backup (the newest "...-before-restore.dump" in backups\) with -BackupFile.')
    } finally {
        [void](Invoke-Quiet 'docker' @('exec', $PostgresContainer, 'rm', '-f', $ContainerDumpPath))
    }

    Write-Step 'Bringing the tables up to date (cs2bot db upgrade)'
    Invoke-Checked 'cs2bot db upgrade' 'uv' @('run', 'cs2bot', 'db', 'upgrade')

    Write-Host ''
    Write-Host 'Restore finished.' -ForegroundColor Green
    Write-Host '  * Check the kill switch / pause state and the bankroll on the dashboard after starting the bot.'
    Write-Host '  * Compare open bets with "Moje zaklady" on Betclic.'
    Write-Host '  * Start the bot: deploy\windows\start.cmd'
    exit 0
} catch {
    Write-Fail $_.Exception.Message
    exit 1
} finally {
    Exit-StartLock
}
