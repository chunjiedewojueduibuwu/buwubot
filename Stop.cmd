@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Manage-BuwuBot.ps1" -Action Stop %*
if errorlevel 1 pause
