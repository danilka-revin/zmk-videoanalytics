@echo off
cd /d "%~dp0\.."
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1"
if errorlevel 1 pause
