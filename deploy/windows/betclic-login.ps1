<#
.SYNOPSIS
    Opens the bot's own browser window on Betclic.pl so you can log in by hand (incl. SMS / 2FA).

.DESCRIPTION
    The bot uses its own Chromium with its own profile (data\browser\betclic), separate from your
    normal browser. You log in there once; the profile keeps the session. Log in again whenever
    the dashboard says "Betclic login required" (the bot pauses betting until then).

    * Bot NOT running: runs "uv run cs2bot executor login" and waits until you have logged in or
      closed the window.
    * Bot running: the bot's browser profile is in use, so a second browser cannot open it. The
      script asks the running bot to open its login window instead (the same as the "Betclic
      login" button on the dashboard) and waits for the result.

    Tick "remember me" (Zapamietaj mnie) on the Betclic login page. The script never sees or
    stores your password.

.PARAMETER TimeoutMinutes
    How long to wait for the login when the bot is running (default 15).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\betclic-login.ps1
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 120)][int]$TimeoutMinutes = 15
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

function Invoke-BotApi {
    # Calls the local dashboard API. Returns @{ Status = <int>; Body = <object or $null> }.
    # Status 0 = no answer. POST requests carry the header the API requires (X-Requested-With).
    param(
        [Parameter(Mandatory = $true)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $uri = (Get-DashboardUrl) + $Path
    try {
        $response = Invoke-WebRequest -Uri $uri -Method $Method -Headers @{ 'X-Requested-With' = 'cs2bot' } `
            -UseBasicParsing -TimeoutSec 15
        $body = $null
        if (-not [string]::IsNullOrWhiteSpace($response.Content)) { $body = $response.Content | ConvertFrom-Json }
        return @{ Status = [int]$response.StatusCode; Body = $body }
    } catch [System.Net.WebException] {
        $webResponse = $_.Exception.Response
        if ($null -eq $webResponse) { return @{ Status = 0; Body = $null } }
        return @{ Status = [int]$webResponse.StatusCode; Body = $null }
    }
}

function Start-LoginThroughRunningBot {
    Write-Step 'The bot is running - asking it to open its Betclic login window'
    $result = Invoke-BotApi -Method 'POST' -Path '/api/control/betclic-login'
    switch ($result.Status) {
        202 { Write-Ok 'The login window is opening (it can take a few seconds).' }
        409 { Write-Info 'A login window is already open - use it.' }
        401 {
            throw ('The dashboard is password-protected (CS2BOT_API__PASSWORD is set), so this script cannot ask the bot. ' +
                "Open $(Get-DashboardUrl), log in and press the 'Betclic login' button instead.")
        }
        501 {
            throw ('The running bot cannot open a login window (no login launcher configured). ' +
                'Stop the bot (stop.cmd), run this script again, then start the bot.')
        }
        0 { throw "The bot's dashboard at $(Get-DashboardUrl) does not answer. Wait until the bot has fully started, or check the log in logs\." }
        default { throw "The dashboard answered with HTTP $($result.Status). Use the 'Betclic login' button on the dashboard instead." }
    }

    Write-Host ''
    Write-Host 'Log in to Betclic in the browser window the bot opened:' -ForegroundColor Cyan
    Write-Host '  1. Enter your login and password, tick "remember me", confirm the SMS / 2FA code.'
    Write-Host '  2. Wait until you see your balance in the page header.'
    Write-Host '  3. Leave the window open - the bot uses it to place bets.'
    Write-Host ''

    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $state = Invoke-BotApi -Method 'GET' -Path '/api/control/betclic-login'
        if ($state.Status -ne 200 -or $null -eq $state.Body) { continue }
        if ($state.Body.running) { continue }
        if ($state.Body.last_result -eq $true) {
            Write-Ok 'Logged in. If betting was paused because of the login, press "Resume" on the dashboard.'
            return
        }
        if ($null -ne $state.Body.last_error) {
            throw "The login window failed: $($state.Body.last_error)"
        }
        throw 'The login was not completed (the window was closed or timed out). Run this script again.'
    }
    throw "No login within $TimeoutMinutes minutes. Run this script again when you are ready."
}

function Start-LoginStandalone {
    Write-Step 'Making sure PostgreSQL is running'
    Start-DockerDesktop
    Start-Postgres

    Write-Step 'Opening the bot''s browser on Betclic (uv run cs2bot executor login)'
    Write-Host '  1. Enter your login and password, tick "remember me", confirm the SMS / 2FA code.'
    Write-Host '  2. Wait until you see your balance in the page header - the window then closes by itself.'
    Write-Host ''
    & uv run cs2bot executor login
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "The login did not complete (exit code $code). Run this script again; if it keeps failing see 'Betclic login' in docs\WINDOWS_SETUP.md."
    }
    Write-Ok 'Logged in. The session is stored in the bot''s browser profile (data\browser\betclic).'
}

# ---------------------------------------------------------------------------------------------

try {
    Assert-RepoReady
    if (Test-BotRunning) {
        Start-LoginThroughRunningBot
    } else {
        Start-LoginStandalone
        Write-Host ''
        Write-Host 'Next: start the bot with deploy\windows\start.cmd' -ForegroundColor Green
    }
    exit 0
} catch {
    Write-Fail $_.Exception.Message
    exit 1
}
