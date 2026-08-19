@echo off
setlocal
cd /d "%~dp0\.."
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-windows.ps1" %*
if errorlevel 1 pause
endlocal
