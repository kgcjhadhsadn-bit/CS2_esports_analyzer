@echo off
rem Removes the automatic start.
rem Double-click this file, or run it from a command prompt. Extra arguments are passed to unregister-autostart.ps1.
rem -ExecutionPolicy Bypass applies to this one PowerShell run only; no system setting is changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0unregister-autostart.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
