<#
.SYNOPSIS
    Internal helper of stop.ps1: sends Ctrl+C to the console that a given process runs in.

.DESCRIPTION
    Windows has no "please stop" signal for another program's console. The documented way is:
    detach from our own console, attach to the target's console, ignore Ctrl+C ourselves and
    generate a Ctrl+C event for everything attached to that console. For the bot this is exactly
    what pressing Ctrl+C in its window does: "cs2bot up" finishes what it is doing, closes the
    browser and exits.

    stop.ps1 runs this in a separate, hidden PowerShell process so that its own window keeps its
    console. Do not run it by hand.

    Exit codes: 0 = signal sent, 2 = could not attach to the process's console (the process is
    gone or has no console), 3 = the signal could not be generated.

.PARAMETER ProcessId
    Id of any process attached to the target console (the bot's python.exe).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$ProcessId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    Add-Type -Namespace 'Cs2Bot' -Name 'ConsoleSignal' -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool AttachConsole(uint dwProcessId);

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool FreeConsole();

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool SetConsoleCtrlHandler(IntPtr handlerRoutine, bool add);

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GenerateConsoleCtrlEvent(uint dwCtrlEvent, uint dwProcessGroupId);
'@

    # 1. Leave our own (hidden) console; a process can be attached to one console only.
    [void][Cs2Bot.ConsoleSignal]::FreeConsole()
    # 2. Join the console of the bot.
    if (-not [Cs2Bot.ConsoleSignal]::AttachConsole([uint32]$ProcessId)) { exit 2 }
    # 3. Make this helper ignore the Ctrl+C it is about to send.
    [void][Cs2Bot.ConsoleSignal]::SetConsoleCtrlHandler([IntPtr]::Zero, $true)
    # 4. CTRL_C_EVENT (0) to every process attached to this console (process group 0).
    $sent = [Cs2Bot.ConsoleSignal]::GenerateConsoleCtrlEvent([uint32]0, [uint32]0)
    # 5. Stay attached for a moment so the event is delivered before we leave.
    Start-Sleep -Seconds 2
    [void][Cs2Bot.ConsoleSignal]::FreeConsole()
    if (-not $sent) { exit 3 }
    exit 0
} catch {
    exit 1
}
