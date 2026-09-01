@echo off
setlocal
cd /d "%~dp0"

rem =====================================================================
rem ZMK Vision - Windows "one click" demo launcher
rem
rem Double-click this file:
rem   * first run  -> runs the Windows installer once (installs/starts
rem                   Docker Desktop, creates .env, enables inference,
rem                   opens the dashboard);
rem   * next runs  -> starts the already-installed stack with one click.
rem
rem Use ZMK_NO_AUTO_UPDATE=1 here so a local build/feature branch is never
rem silently replaced by the latest GitHub release during a demo.
rem =====================================================================

set ZMK_NO_AUTO_UPDATE=1
set ENABLE_INFERENCE=true

if exist ".env" if exist ".zmk-profiles" goto start

:install
echo [start-demo] First run detected. Running Windows installer (one time)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installers\install-windows.ps1" %*
if errorlevel 1 goto fail
goto end

:start
echo [start-demo] Starting ZMK Vision...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 goto fail
goto end

:fail
echo.
echo [start-demo] ZMK Vision failed to start.
echo Common fixes:
echo   - Docker Desktop must be running (installer tries to start it).
echo   - If the API takes long to build, wait a minute and run this file again.
echo   - First run requires Docker Desktop to be installed already; if not,
echo     run installers\install-windows.ps1 (or this file) once with internet.
pause
exit /b 1

:end
endlocal
