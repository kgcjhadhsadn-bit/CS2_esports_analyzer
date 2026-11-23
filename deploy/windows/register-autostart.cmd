@echo off
rem Starts the bot automatically when you sign in to Windows.
rem Double-click this file, or run it from a command prompt. Extra arguments are passed to register-autostart.ps1.
rem -ExecutionPolicy Bypass applies to this one PowerShell run only; no system setting is changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0register-autostart.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
