<#
.SYNOPSIS
    Installs (or repairs) cs2bot on this Windows PC. Safe to run again at any time.

.DESCRIPTION
    Steps, each skipped when it is already done:
      1. Checks the prerequisites and offers to install the missing ones with winget:
         Git, Docker Desktop, Node.js LTS (dashboard build) and 7-Zip (HLTV demo archives are .rar).
         uv (the Python package manager; it also downloads Python itself) comes from its official
         installer.
      2. Starts Docker Desktop.
      3. Creates .env from .env.example (with a random database password) and opens it in Notepad.
      4. Installs the Python packages (uv sync) and the bot's own Chromium (playwright install).
      5. Starts PostgreSQL in Docker and waits until it is healthy.
      6. Creates / upgrades the database tables (cs2bot db upgrade).
      7. Builds the dashboard (npm ci, npm run build) when the dashboard/ folder exists.
      8. Runs "cs2bot doctor" and prints the next steps.

    Nothing here places a bet or logs in to Betclic.

.PARAMETER Yes
    Do not ask before installing prerequisites and do not pause for editing .env (unattended).

.PARAMETER SkipPrerequisites
    Do not check or install Git / Docker Desktop / Node.js / 7-Zip / uv.

.PARAMETER NoEditEnv
    Do not open .env in Notepad.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$SkipPrerequisites,
    [switch]$NoEditEnv
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

function Install-WingetPackage {
    # Offers to install one program with winget. The caller checks afterwards whether it is there:
    # winget's exit code is not reliable (it is non-zero for "already installed", too).
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$DisplayName,
        [Parameter(Mandatory = $true)][string]$ManualUrl
    )
    if (-not (Test-CommandExists 'winget')) {
        throw "$DisplayName is missing and winget is not available on this PC. Install 'App Installer' from the Microsoft Store (it provides winget), or install $DisplayName yourself from $ManualUrl - then run this script again."
    }
    if (-not (Confirm-Action "Install $DisplayName now (winget install $Id)?" -AssumeYes:$Yes)) {
        throw "$DisplayName is required. Install it yourself from $ManualUrl and run this script again."
    }
    & winget install --id $Id --exact --source winget --accept-package-agreements --accept-source-agreements
    Write-Info "winget finished with exit code $LASTEXITCODE."
    Update-SessionPath
}

function New-RandomPassword {
    # Letters and digits only, so the value needs no escaping inside the database URL.
    param([int]$Length = 28)
    $alphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    $bytes = New-Object byte[] $Length
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    $chars = foreach ($b in $bytes) { $alphabet[$b % $alphabet.Length] }
    return (-join $chars)
}

function Test-ArchiveToolPresent {
    if ((Test-CommandExists '7z') -or (Test-CommandExists 'unrar')) { return $true }
    $candidates = @(
        (Join-Path $env:ProgramFiles '7-Zip\7z.exe'),
        (Join-Path $env:ProgramFiles 'WinRAR\UnRAR.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $true }
    }
    return $false
}

function Test-DockerInstalled {
    if (Test-CommandExists 'docker') { return $true }
    return (Test-Path -LiteralPath (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'))
}

function Install-Prerequisites {
    Write-Step 'Checking prerequisites'
    Update-SessionPath

    if (Test-CommandExists 'git') {
        Write-Ok 'Git is installed.'
    } else {
        Install-WingetPackage -Id 'Git.Git' -DisplayName 'Git' -ManualUrl 'https://git-scm.com/download/win'
        if (-not (Test-CommandExists 'git')) {
            Write-Warn 'Git is not visible in this window yet. If updating fails later, open a new PowerShell window.'
        }
    }

    $dashboardPresent = Test-Path -LiteralPath (Join-Path $RepoRoot 'dashboard\package.json')
    if (Test-CommandExists 'node') {
        Write-Ok 'Node.js is installed.'
    } elseif ($dashboardPresent) {
        Install-WingetPackage -Id 'OpenJS.NodeJS.LTS' -DisplayName 'Node.js LTS' -ManualUrl 'https://nodejs.org/'
        if (-not (Test-CommandExists 'npm.cmd')) {
            throw 'Node.js was installed but is not visible in this window. Close PowerShell, open a new window and run this script again.'
        }
    } else {
        Write-Info 'Node.js is not installed. It is only needed to build the dashboard, which is not in this checkout yet.'
    }

    if (Test-ArchiveToolPresent) {
        Write-Ok '7-Zip (or WinRAR) is installed - needed to unpack HLTV demo archives.'
    } else {
        Install-WingetPackage -Id '7zip.7zip' -DisplayName '7-Zip' -ManualUrl 'https://www.7-zip.org/'
        if (-not (Test-ArchiveToolPresent)) {
            Write-Warn '7-Zip was not found after the installation. Demo parsing will not work until it is installed ("cs2bot doctor" checks this).'
        }
    }

    if (Test-CommandExists 'uv') {
        Write-Ok 'uv is installed.'
    } else {
        if (-not (Confirm-Action 'Install uv (Python package manager) with its official installer from astral.sh?' -AssumeYes:$Yes)) {
            throw 'uv is required. See https://docs.astral.sh/uv/getting-started/installation/ and run this script again.'
        }
        # Official installer, run in a separate PowerShell so its settings cannot affect this script.
        $installCommand = '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command $installCommand
        if ($LASTEXITCODE -ne 0) {
            throw "The uv installer failed (exit code $LASTEXITCODE). Alternative: winget install --id astral-sh.uv -e"
        }
        Update-SessionPath
        if (-not (Test-CommandExists 'uv')) {
            throw 'uv was installed but is not visible in this window. Close PowerShell, open a new window and run this script again.'
        }
        Write-Ok 'uv installed.'
    }

    if (Test-DockerInstalled) {
        Write-Ok 'Docker Desktop is installed.'
    } else {
        Install-WingetPackage -Id 'Docker.DockerDesktop' -DisplayName 'Docker Desktop' -ManualUrl 'https://www.docker.com/products/docker-desktop/'
        Write-Host ''
        Write-Host 'Docker Desktop has just been installed. Before this script can continue:' -ForegroundColor Yellow
        Write-Host '  1. Restart Windows if the Docker installer asked for it (it sets up WSL 2).'
        Write-Host '  2. Open "Docker Desktop" from the Start menu, accept the terms, skip the sign-in.'
        Write-Host '  3. Wait until it shows "Engine running".'
        Write-Host '  4. Run this script again - it continues where it stopped.'
        exit 2
    }
}

function Show-DiskSpace {
    Write-Step 'Checking free disk space'
    $systemDriveName = $env:SystemDrive.TrimEnd(':')
    $repoDriveName = (Get-Item -LiteralPath $RepoRoot).PSDrive.Name
    $names = @($systemDriveName)
    if ($repoDriveName -ne $systemDriveName) { $names += $repoDriveName }
    foreach ($name in $names) {
        $drive = Get-PSDrive -Name $name -ErrorAction SilentlyContinue
        if ($null -eq $drive -or $null -eq $drive.Free) { continue }
        $freeGb = [math]::Round($drive.Free / 1GB)
        if ($freeGb -lt 100) {
            Write-Warn "Drive ${name}: has $freeGb GB free. Plan for 100-200 GB (database + temporary demo files). The bot works with less, but the full history backfill will not fit."
        } else {
            Write-Ok "Drive ${name}: has $freeGb GB free."
        }
    }
    Write-Info 'The database lives inside Docker Desktop''s disk image, by default on the system drive.'
    Write-Info 'To move it: Docker Desktop > Settings > Resources > Advanced > Disk image location.'
}

function Initialize-EnvFile {
    # Returns $true when .env was created by this run.
    Write-Step 'Preparing the settings file (.env)'
    if (Test-Path -LiteralPath $EnvFile) {
        Write-Ok '.env already exists - left unchanged.'
        return $false
    }
    if (-not (Test-Path -LiteralPath $EnvExampleFile)) {
        throw ".env.example is missing in $RepoRoot. Is this a complete copy of the repository?"
    }
    Copy-Item -LiteralPath $EnvExampleFile -Destination $EnvFile

    # A brand-new database gets a random password instead of the "change-me" default. PostgreSQL
    # only applies the password when its data volume is first created, so an existing volume keeps
    # whatever password it was created with.
    $volumeExists = ((Invoke-Quiet 'docker' @('volume', 'inspect', $PostgresVolume)) -eq 0)
    if ($volumeExists) {
        Write-Warn "A database volume ($PostgresVolume) from an earlier installation exists."
        Write-Warn 'The password in CS2BOT_DATABASE__URL must be the one that database was created with (the default was "change-me").'
    } else {
        $text = [System.IO.File]::ReadAllText($EnvFile)
        $marker = 'cs2bot:change-me@'
        if ($text.Contains($marker)) {
            $password = New-RandomPassword
            $newline = "`n"
            if ($text.Contains("`r`n")) { $newline = "`r`n" }
            $text = $text.Replace($marker, "cs2bot:$password@")
            $text = $text.TrimEnd() + $newline + $newline +
                '# --- PostgreSQL container (docker-compose.yml) -------------------------------------' + $newline +
                '# Generated by install.ps1. Must be the same password as in CS2BOT_DATABASE__URL above.' + $newline +
                '# It is only applied when the database is created; changing it later breaks the login.' + $newline +
                "POSTGRES_PASSWORD=$password" + $newline
            [System.IO.File]::WriteAllText($EnvFile, $text, (New-Object System.Text.UTF8Encoding($false)))
            Write-Ok 'Created .env with a random database password.'
        } else {
            Write-Ok 'Created .env from .env.example.'
        }
    }
    return $true
}

function Edit-EnvFile {
    Write-Step 'Fill in your keys and limits'
    Write-Info 'Notepad opens .env now. Fill in at least:'
    Write-Info '  CS2BOT_PANDASCORE__API_TOKEN        your PandaScore token'
    Write-Info '  ANTHROPIC_API_KEY                   your Anthropic (Claude) API key'
    Write-Info '  CS2BOT_BANKROLL__STARTING_BANKROLL  the money (PLN) the bot may work with'
    Write-Info 'Leave CS2BOT_EXECUTOR__DRY_RUN=true for the first test. Never paste these values into a chat.'
    Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + $EnvFile + '"') | Out-Null
    [void](Read-Host 'Save the file (Ctrl+S), close Notepad, then press Enter here to continue')
}

function Show-EnvWarnings {
    $missing = @()
    if ((Get-EnvFileValue -Name 'CS2BOT_PANDASCORE__API_TOKEN') -eq '') { $missing += 'CS2BOT_PANDASCORE__API_TOKEN (fixtures, results)' }
    $anthropic = Get-EnvFileValue -Name 'ANTHROPIC_API_KEY'
    if ($anthropic -eq '') { $anthropic = Get-EnvFileValue -Name 'CS2BOT_LLM__API_KEY' }
    if ($anthropic -eq '') { $missing += 'ANTHROPIC_API_KEY (news context and rationales)' }
    foreach ($item in $missing) {
        Write-Warn "Not filled in yet: $item. Edit .env (notepad .env) before starting the bot."
    }
    if (-not (Test-DryRunEnabled)) {
        Write-Warn 'CS2BOT_EXECUTOR__DRY_RUN=false: the bot will place REAL bets once it runs and you are logged in to Betclic.'
    }
}

# ---------------------------------------------------------------------------------------------

try {
    Write-Host 'cs2bot installer' -ForegroundColor Cyan
    Write-Host "Folder: $RepoRoot"

    if ($env:OS -ne 'Windows_NT') { throw 'This script is for Windows. On Linux/macOS use scripts/dev.sh.' }
    if ($PSVersionTable.PSVersion.Major -lt 5) { throw 'Windows PowerShell 5.1 or newer is required.' }
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'A 64-bit Windows is required.' }
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml'))) {
        throw "pyproject.toml not found in $RepoRoot. Run this script from inside the cloned repository."
    }
    if ($RepoRoot -match 'OneDrive') {
        Write-Warn 'This folder is inside OneDrive. Syncing the database tools, browser profile and logs causes locked files - move the repository to a plain folder such as C:\cs2bot.'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warn 'This window runs as Administrator. That is not needed; a normal PowerShell window is safer (installers ask for permission themselves).'
    }
    Set-Location -LiteralPath $RepoRoot

    if ($SkipPrerequisites) {
        Update-SessionPath
    } else {
        Install-Prerequisites
    }
    foreach ($tool in @('uv', 'docker')) {
        if (-not (Test-CommandExists $tool)) { throw "'$tool' was not found on PATH. Open a new PowerShell window and run this script again." }
    }
    Show-DiskSpace

    Write-Step 'Starting Docker Desktop'
    Start-DockerDesktop
    Write-Ok 'Docker is ready.'

    $envCreated = Initialize-EnvFile
    if ($envCreated -and -not $NoEditEnv -and -not $Yes) { Edit-EnvFile }
    Import-AnthropicApiKey
    Show-EnvWarnings

    foreach ($folder in @($LogDir, $BackupDir, $RunDir)) { Initialize-RuntimeFolder -Path $folder }

    Write-Step 'Installing Python and the Python packages (uv sync) - the first run downloads about 1 GB'
    Invoke-Checked 'uv sync' 'uv' @('sync', '--frozen')
    Write-Ok 'Python environment ready (.venv).'

    Write-Step 'Installing the bot''s own Chromium browser (playwright install chromium)'
    Invoke-Checked 'playwright install chromium' 'uv' @('run', 'playwright', 'install', 'chromium')
    Write-Ok 'Chromium installed.'

    Write-Step 'Starting PostgreSQL in Docker (the first start downloads the image)'
    Start-Postgres -TimeoutSeconds 300

    Write-Step 'Creating / upgrading the database tables (cs2bot db upgrade)'
    Invoke-Checked 'cs2bot db upgrade' 'uv' @('run', 'cs2bot', 'db', 'upgrade')
    Write-Ok 'Database schema is up to date.'

    Write-Step 'Building the dashboard'
    Build-Dashboard

    Write-Step 'Checking the installation (cs2bot doctor)'
    & uv run cs2bot doctor
    if ($LASTEXITCODE -ne 0) {
        Write-Warn 'The doctor reported problems (see above). Fix them, then run: uv run cs2bot doctor'
    } else {
        Write-Ok 'The doctor found no problems.'
    }

    Write-Host ''
    Write-Host 'Installation finished.' -ForegroundColor Green
    Write-Host ''
    Write-Host 'Next steps (details: docs\WINDOWS_SETUP.md):'
    Write-Host '  1. Log in to Betclic once in the bot''s own browser window (incl. SMS / 2FA):'
    Write-Host '       deploy\windows\betclic-login.cmd'
    Write-Host '  2. Start the bot:'
    Write-Host '       deploy\windows\start.cmd'
    Write-Host "  3. Open the dashboard:  $(Get-DashboardUrl)"
    Write-Host '  4. Watch a dry run first (CS2BOT_EXECUTOR__DRY_RUN=true places no real bets).'
    Write-Host '     To go live set CS2BOT_EXECUTOR__DRY_RUN=false in .env and restart the bot.'
    Write-Host '  5. Optional: start automatically at sign-in: deploy\windows\register-autostart.cmd'
    Write-Host '  6. Optional: daily database backup:          deploy\windows\backup-db.cmd -Schedule 04:30'
    Write-Host '  7. Keep the PC awake before matches:         deploy\windows\power-settings.cmd'
    exit 0
} catch {
    Write-Fail $_.Exception.Message
    Write-Host 'Nothing was placed or changed on Betclic. Fix the problem above and run this script again.'
    exit 1
}
