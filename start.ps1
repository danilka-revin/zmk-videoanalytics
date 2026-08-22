# =====================================================================
# ZMK Vision launcher (Windows).
#
# On every start this launcher:
#   1. Checks for a newer GitHub release.
#   2. If one exists: downloads it, verifies the SHA256 checksum,
#      unpacks it and re-launches this same script with the new build.
#   3. Otherwise (or after an update) it starts the stack with Docker.
#
# Set $env:ZMK_NO_AUTO_UPDATE='1' to skip the version check (offline).
# =====================================================================
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path (Join-Path $Root 'installers\auto-update.ps1'))) { throw "Missing installers\auto-update.ps1 — run install-windows.ps1 first." }
if (-not ($env:ZMK_NO_AUTO_UPDATE -eq '1' -or $env:ZMK_RELAUNCHED_AFTER_UPDATE -eq '1')) {
  & (Join-Path $Root 'installers\auto-update.ps1') -Relaunch start.ps1
}

$required = @('docker-compose.yml', '.env.example', 'backend/Dockerfile', 'frontend/Dockerfile')
foreach ($f in $required) { if (-not (Test-Path $f)) { throw "Missing $f — run install-windows.ps1 first." } }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI is not installed." }
if (-not (docker info 2>$null)) { throw "Docker Desktop is not running. Start it and rerun." }
docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker Compose plugin is unavailable." }

$ComposeProfile = @()
if (Test-Path '.zmk-profiles') { $ComposeProfile = @(Get-Content '.zmk-profiles') }

$runtimes = docker info --format '{{json .Runtimes}}' 2>$null
if ($runtimes -match 'nvidia') {
  $env:COMPOSE_FILE = 'docker-compose.yml;docker-compose.gpu.yml'
  Write-Host 'NVIDIA Container Runtime found: GPU enabled' -ForegroundColor Green
} else {
  Write-Host 'NVIDIA runtime not found: workers use CPU fallback' -ForegroundColor Yellow
}

function Wait-Http([string]$Url, [int]$Seconds = 120) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $Url; if ($r.StatusCode -eq 200) { return $true } } catch {}
    Start-Sleep 2
  }
  return $false
}

Write-Host '[start] Starting ZMK Vision services...'
docker compose @ComposeProfile config --quiet
if ($LASTEXITCODE -ne 0) { throw 'docker-compose.yml or .env validation failed' }
docker compose @ComposeProfile up -d --build --remove-orphans
if ($LASTEXITCODE -ne 0) { docker compose @ComposeProfile logs --tail=100; throw 'docker compose failed' }
if (-not (Wait-Http 'http://localhost:8000/api/health' 120)) { docker compose logs --tail=100 api; throw 'API health check failed' }
if (-not (Wait-Http 'http://localhost:5173' 120)) { docker compose logs --tail=100 web; throw 'Web health check failed' }

Write-Host 'ZMK Vision is running.' -ForegroundColor Green
Write-Host 'Dashboard: http://localhost:5173'
Write-Host 'API docs:  http://localhost:8000/docs'
Start-Process 'http://localhost:5173'
