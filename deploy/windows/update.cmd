@echo off
rem Updates cs2bot (git pull, packages, database tables, dashboard).
rem Double-click this file, or run it from a command prompt. Extra arguments are passed to update.ps1.
rem -ExecutionPolicy Bypass applies to this one PowerShell run only; no system setting is changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
