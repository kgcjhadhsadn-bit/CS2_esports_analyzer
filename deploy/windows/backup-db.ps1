<#
.SYNOPSIS
    Backs up the cs2bot database to backups\cs2bot-YYYYMMDD-HHMMSS.dump and deletes old backups.

.DESCRIPTION
    Runs pg_dump inside the PostgreSQL container (docker exec; no PostgreSQL tools are needed on
    Windows) in PostgreSQL's compressed "custom" format, checks that the file can be read back
    (pg_restore --list), and only then gives it its final name. Backups older than -KeepDays are
    deleted, but the newest -KeepAtLeast backups are always kept. Restore with restore-db.ps1.

    The bot can keep running while the backup is made (pg_dump takes a consistent snapshot).

    The backups stay on this PC. Copy the backups folder to an external drive or a cloud folder
    from time to time: a dead disk takes the database AND its backups with it.

    -Schedule HH:mm registers a daily Task Scheduler entry ("cs2bot-backup") that runs this script
    at that time (or as soon as possible after it, if the PC was off); -Unschedule removes it.

.PARAMETER KeepDays
    Delete backups older than this many days (default 14).

.PARAMETER KeepAtLeast
    Never delete the newest N backups, however old (default 3).

.PARAMETER Label
    Optional word added to the file name, e.g. "before-update" (letters, digits and dashes).

.PARAMETER NoPrune
    Do not delete any old backups in this run.

.PARAMETER Schedule
    Register a daily backup at this local time (HH:mm, e.g. 04:30) instead of making a backup now.

.PARAMETER Unschedule
    Remove the daily backup task.

.PARAMETER Background
    For the scheduled task: no console output (results go to logs\backup.log).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\backup-db.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\backup-db.ps1 -Schedule 04:30
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 3650)][int]$KeepDays = 14,
    [ValidateRange(1, 1000)][int]$KeepAtLeast = 3,
    [ValidatePattern('^[A-Za-z0-9-]{0,40}$')][string]$Label = '',
    [switch]$NoPrune,
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string]$Schedule = '',
    [switch]$Unschedule,
    [switch]$Background
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$BackupTaskName = 'cs2bot-backup'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-BackupLog {
    # One line in logs\backup.log (and on screen unless -Background).
    param([Parameter(Mandatory = $true)][string]$Message)
    try {
        Initialize-RuntimeFolder -Path $LogDir
        $line = '{0} {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'), $Message
        [System.IO.File]::AppendAllText((Join-Path $LogDir 'backup.log'), $line + "`r`n", $Utf8NoBom)
    } catch {
        Write-Verbose 'Could not write logs\backup.log.'
    }
    if (-not $Background) { Write-Info $Message }
}

function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

function Register-BackupTask {
    $parts = $Schedule.Split(':')
    $at = (Get-Date).Date.AddHours([int]$parts[0]).AddMinutes([int]$parts[1])
    $trigger = New-ScheduledTaskTrigger -Daily -At $at
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
    $action = New-ScriptTaskAction -ScriptName 'backup-db.ps1' -ScriptArguments "-Background -KeepDays $KeepDays -KeepAtLeast $KeepAtLeast"
    Register-UserTask -TaskName $BackupTaskName -Description 'cs2bot: daily database backup (deploy\windows\backup-db.ps1)' `
        -Action $action -Trigger $trigger -Settings $settings
    Write-Ok "Daily backup scheduled at $Schedule (Task Scheduler entry '$BackupTaskName')."
    Write-Info 'If the PC is off at that time, the backup runs soon after you next sign in.'
    Write-Info 'Results: logs\backup.log. Remove the entry with: backup-db.ps1 -Unschedule'
}

function Unregister-BackupTask {
    $task = Get-ScheduledTask -TaskName $BackupTaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Info "There is no '$BackupTaskName' task."
        return
    }
    Unregister-ScheduledTask -TaskName $BackupTaskName -Confirm:$false
    Write-Ok "Removed the daily backup task '$BackupTaskName'."
}

function Remove-OldBackups {
    # Deletes cs2bot-*.dump files older than $KeepDays, never touching the newest $KeepAtLeast.
    $all = @(Get-ChildItem -LiteralPath $BackupDir -Filter 'cs2bot-*.dump' -File -ErrorAction SilentlyContinue |
        Sort-Object -Property LastWriteTime -Descending)
    $cutoff = (Get-Date).AddDays(-$KeepDays)
    $removed = 0
    for ($i = $KeepAtLeast; $i -lt $all.Count; $i++) {
        if ($all[$i].LastWriteTime -lt $cutoff) {
            Remove-Item -LiteralPath $all[$i].FullName -Force
            $removed += 1
        }
    }
    # Partial files of interrupted runs.
    foreach ($partial in @(Get-ChildItem -LiteralPath $BackupDir -Filter '*.partial' -File -ErrorAction SilentlyContinue)) {
        if ($partial.LastWriteTime -lt (Get-Date).AddDays(-1)) { Remove-Item -LiteralPath $partial.FullName -Force }
    }
    if ($removed -gt 0) { Write-BackupLog "Deleted $removed backup(s) older than $KeepDays days." }
}

function New-DatabaseBackup {
    $identity = Get-PostgresIdentity
    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
    $name = "cs2bot-$stamp"
    if ($Label -ne '') { $name = "$name-$Label" }
    $final = Join-Path $BackupDir "$name.dump"
    $partial = "$final.partial"
    $errorFile = Join-Path $RunDir 'pg_dump.err'
    $listFile = Join-Path $RunDir 'pg_restore-list.txt'

    # pg_dump writes the archive to its standard output, which goes straight into the file
    # (binary-safe; see Invoke-ProcessToFile). The container trusts local connections, so no
    # password is involved.
    $dumpArgs = @('exec', $PostgresContainer, 'pg_dump', '--username', $identity.User, '--dbname', $identity.Database,
        '--format=custom', '--compress=6', '--no-password')
    $code = Invoke-ProcessToFile -FilePath 'docker' -Arguments $dumpArgs -OutputFile $partial -ErrorFile $errorFile
    if ($code -ne 0) {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        throw "pg_dump failed (exit code $code): $(Get-FileTail -Path $errorFile)"
    }
    $size = (Get-Item -LiteralPath $partial).Length
    if ($size -lt 1024) {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        throw "The backup file is only $size bytes - something went wrong: $(Get-FileTail -Path $errorFile)"
    }

    # Read the archive back: pg_restore --list reads its table of contents from standard input.
    $verifyArgs = @('exec', '-i', $PostgresContainer, 'pg_restore', '--list')
    $code = Invoke-ProcessToFile -FilePath 'docker' -Arguments $verifyArgs -OutputFile $listFile -ErrorFile $errorFile -InputFile $partial
    if ($code -ne 0) {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        throw "The backup could not be read back (pg_restore --list exit code $code): $(Get-FileTail -Path $errorFile)"
    }
    Remove-Item -LiteralPath $listFile, $errorFile -Force -ErrorAction SilentlyContinue
    Move-Item -LiteralPath $partial -Destination $final
    Write-BackupLog "Backup written: $final ($(Format-Size $size))."
    return $final
}

# ---------------------------------------------------------------------------------------------

try {
    if ($Schedule -ne '' -and $Unschedule) { throw 'Use either -Schedule or -Unschedule, not both.' }
    if ($Unschedule) {
        Unregister-BackupTask
        exit 0
    }
    Assert-RepoReady
    if ($Schedule -ne '') {
        Register-BackupTask
        exit 0
    }

    foreach ($folder in @($BackupDir, $RunDir, $LogDir)) { Initialize-RuntimeFolder -Path $folder }
    if (-not $Background) { Write-Step 'Making sure PostgreSQL is running' }
    $dockerTimeout = 300
    if ($Background) { $dockerTimeout = 900 }
    Start-DockerDesktop -TimeoutSeconds $dockerTimeout
    Start-Postgres

    if (-not $Background) { Write-Step 'Backing up the database (pg_dump)' }
    $file = New-DatabaseBackup
    if (-not $NoPrune) { Remove-OldBackups }
    if (-not $Background) {
        Write-Ok "Backup finished: $file"
        Write-Info 'Copy the backups folder to an external drive or cloud folder now and then.'
    }
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-BackupLog "BACKUP FAILED: $message"
    if (-not $Background) { Write-Fail $message }
    exit 1
}
