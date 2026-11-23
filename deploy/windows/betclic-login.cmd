@echo off
rem Opens the bot's browser so you can log in to Betclic by hand (incl. 2FA).
rem Double-click this file, or run it from a command prompt. Extra arguments are passed to betclic-login.ps1.
rem -ExecutionPolicy Bypass applies to this one PowerShell run only; no system setting is changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0betclic-login.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
