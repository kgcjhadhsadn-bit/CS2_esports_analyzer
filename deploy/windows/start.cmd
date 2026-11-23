@echo off
rem Starts the bot (Docker, PostgreSQL, cs2bot up). Keep this window open; Ctrl+C stops the bot.
rem Double-click this file, or run it from a command prompt. Extra arguments are passed to start.ps1.
rem -ExecutionPolicy Bypass applies to this one PowerShell run only; no system setting is changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
