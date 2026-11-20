@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0BladeQuiet.ps1" -Action Quiet
if errorlevel 1 pause
