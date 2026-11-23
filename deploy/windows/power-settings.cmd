@echo off
rem Shows / changes the sleep settings (asks before every change).
rem Double-click this file, or run it from a command prompt. Extra arguments are passed to power-settings.ps1.
rem -ExecutionPolicy Bypass applies to this one PowerShell run only; no system setting is changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0power-settings.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
