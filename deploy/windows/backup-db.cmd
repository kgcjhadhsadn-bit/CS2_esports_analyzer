@echo off
rem Backs up the database into the backups folder.
rem Double-click this file, or run it from a command prompt. Extra arguments are passed to backup-db.ps1.
rem -ExecutionPolicy Bypass applies to this one PowerShell run only; no system setting is changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0backup-db.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
