@echo off
setlocal
cd /d "%~dp0\.."
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" %*
if errorlevel 1 (
  echo.
  echo ZMK Vision installation failed. Review the error above.
  pause
  exit /b 1
)
endlocal
