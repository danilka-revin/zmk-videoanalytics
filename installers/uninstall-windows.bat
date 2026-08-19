@echo off
cd /d "%~dp0\.."
docker compose --profile telegram --profile production down
pause
