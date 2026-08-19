#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "ZMK Vision Installer"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
Write-Host "`n=== ZMK Vision one-click installer for Windows ===" -ForegroundColor Green

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host "Docker Desktop not found. Installing via winget..." -ForegroundColor Yellow
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Install Docker Desktop from https://docker.com/products/docker-desktop and rerun." }
  winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
  $dockerPath = "$Env:ProgramFiles\Docker\Docker\resources\bin"
  $Env:Path += ";$dockerPath"
}
if (-not (docker info 2>$null)) {
  Write-Host "Starting Docker Desktop..."
  $desktop = "$Env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
  if (Test-Path $desktop) { Start-Process $desktop }
  $ready = $false
  for ($i=0; $i -lt 60; $i++) { Start-Sleep 3; if (docker info 2>$null) { $ready=$true; break }; Write-Host -NoNewline "." }
  if (-not $ready) { throw "Docker engine did not start. Start Docker Desktop and rerun installer." }
}
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$token = Read-Host "Telegram bot token (Enter to skip bot)"
if ($token) {
  $admin = Read-Host "Your Telegram numeric ID (admin)"
  $url = Read-Host "Public HTTPS Mini App URL (or Enter for localhost)"
  Add-Content .env "`nTELEGRAM_BOT_TOKEN=$token`nTELEGRAM_ADMIN_IDS=$admin"
  if ($url) { Add-Content .env "`nTELEGRAM_WEBAPP_URL=$url" }
  docker compose --profile telegram up -d --build
} else { docker compose up -d --build }
if ($LASTEXITCODE -ne 0) { throw "docker compose failed" }
Write-Host "`nZMK Vision installed successfully." -ForegroundColor Green
Write-Host "Dashboard: http://localhost:5173"
Write-Host "API docs:  http://localhost:8000/docs"
Start-Process "http://localhost:5173"
Read-Host "Press Enter to close"
