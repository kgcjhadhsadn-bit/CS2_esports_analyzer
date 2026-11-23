@echo off
rem Restores the database from a backup (asks for confirmation).
rem Double-click this file, or run it from a command prompt. Extra arguments are passed to restore-db.ps1.
rem -ExecutionPolicy Bypass applies to this one PowerShell run only; no system setting is changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore-db.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
