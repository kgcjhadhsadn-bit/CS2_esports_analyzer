@echo off
rem Installs or repairs cs2bot (prerequisites, .env, Python packages, database, dashboard).
rem Double-click this file, or run it from a command prompt. Extra arguments are passed to install.ps1.
rem -ExecutionPolicy Bypass applies to this one PowerShell run only; no system setting is changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
