#Requires -Version 5.1
param([switch]$CheckOnly, [switch]$NonInteractive)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Host.UI.RawUI.WindowTitle = "ZMK Vision Installer"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Assert-ProjectFiles {
  $required = @("docker-compose.yml", ".env.example", "backend/Dockerfile", "frontend/Dockerfile", "services/telegram_bot/Dockerfile", "services/max_bot/Dockerfile")
  foreach ($file in $required) { if (-not (Test-Path $file)) { throw "Missing project file: $file. Download and extract the complete release archive, not only the installer." } }
}
function Set-DotEnvValue([string]$Name, [string]$Value) {
  $path = Join-Path $Root ".env"
  $lines = if (Test-Path $path) { @(Get-Content $path) } else { @() }
  $found = $false
  $updated = foreach ($line in $lines) {
    if ($line -match ("^" + [regex]::Escape($Name) + "=")) { $found = $true; "$Name=$Value" } else { $line }
  }
  if (-not $found) { $updated += "$Name=$Value" }
  [IO.File]::WriteAllLines($path, [string[]]$updated, ([Text.UTF8Encoding]::new($false)))
}
function Wait-Http([string]$Url, [int]$Seconds = 120) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $Url; if ($r.StatusCode -eq 200) { return $true } } catch {}
    Start-Sleep 2
  }
  return $false
}

Write-Host "`n=== ZMK Vision installer for Windows 10/11 ===" -ForegroundColor Green
Assert-ProjectFiles
if ($CheckOnly) {
  Write-Host "Project files: OK"
  if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker compose version
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose plugin is unavailable" }
    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw "docker-compose.yml validation failed" }
    Write-Host "Docker Compose configuration: OK" -ForegroundColor Green
  } else { Write-Warning "Docker is not installed; project file validation only." }
  exit 0
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host "Docker Desktop not found. Installing via winget..." -ForegroundColor Yellow
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Install Docker Desktop from https://docker.com/products/docker-desktop and rerun." }
  winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
  if ($LASTEXITCODE -ne 0) { throw "Docker Desktop installation failed with code $LASTEXITCODE" }
  $Env:Path += ";$Env:ProgramFiles\Docker\Docker\resources\bin"
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI is unavailable after installation. Restart Windows and run the installer again." }
if (-not (docker info 2>$null)) {
  Write-Host "Starting Docker Desktop..."
  $desktop = "$Env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
  if (Test-Path $desktop) { Start-Process $desktop }
  $ready = $false
  for ($i=0; $i -lt 60; $i++) { Start-Sleep 3; if (docker info 2>$null) { $ready=$true; break }; Write-Host -NoNewline "." }
  if (-not $ready) { throw "Docker engine did not start within 180 seconds. Start Docker Desktop and rerun." }
}
docker compose version | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Docker Compose plugin is unavailable. Update Docker Desktop." }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

$messenger = if ($NonInteractive) { if ($env:MESSENGER_PROVIDER) { $env:MESSENGER_PROVIDER } else { "none" } } else {
  Write-Host "Choose messenger: 1 - Telegram, 2 - MAX, 0 - no bot"
  $choice = Read-Host "Your choice [0/1/2]"
  switch ($choice) { "1" { "telegram" } "2" { "max" } default { "none" } }
}
$ComposeProfile = @()
switch ($messenger.ToLowerInvariant()) {
  "telegram" {
    $token = if ($NonInteractive) { $env:TELEGRAM_BOT_TOKEN } else { Read-Host "Telegram bot token" }
    if ($token -notmatch '^\d+:[A-Za-z0-9_-]{20,}$') { throw "Telegram token format is invalid" }
    $admin = if ($NonInteractive) { $env:TELEGRAM_ADMIN_IDS } else { Read-Host "Your Telegram numeric ID (admin)" }
    if ($admin -notmatch '^\d+(,\d+)*$') { throw "Telegram admin ID must contain only numeric IDs separated by commas" }
    $url = if ($NonInteractive) { $env:TELEGRAM_WEBAPP_URL } else { Read-Host "Public HTTPS Mini App URL (Enter for bot-only)" }
    if ($url -and $url -notmatch '^https://') { throw "Telegram Mini App URL must use HTTPS" }
    Set-DotEnvValue "TELEGRAM_BOT_TOKEN" $token; Set-DotEnvValue "TELEGRAM_ADMIN_IDS" $admin; Set-DotEnvValue "TELEGRAM_WEBAPP_URL" $url
    Set-DotEnvValue "MAX_BOT_TOKEN" ""; Set-DotEnvValue "MESSENGER_PROVIDER" "telegram"
    $ComposeProfile = @("--profile", "telegram")
  }
  "max" {
    $token = if ($NonInteractive) { $env:MAX_BOT_TOKEN } else { Read-Host "MAX bot token from @MasterBot" }
    if ($token -notmatch '^[A-Za-z0-9._:-]{20,500}$') { throw "MAX token format is invalid" }
    $admin = if ($NonInteractive) { $env:MAX_ADMIN_IDS } else { Read-Host "Your MAX numeric ID (admin)" }
    if ($admin -notmatch '^\d+(,\d+)*$') { throw "MAX admin ID must contain only numeric IDs separated by commas" }
    Set-DotEnvValue "MAX_BOT_TOKEN" $token; Set-DotEnvValue "MAX_ADMIN_IDS" $admin
    Set-DotEnvValue "TELEGRAM_BOT_TOKEN" ""; Set-DotEnvValue "MESSENGER_PROVIDER" "max"
    $ComposeProfile = @("--profile", "max")
  }
  "none" { Set-DotEnvValue "TELEGRAM_BOT_TOKEN" ""; Set-DotEnvValue "MAX_BOT_TOKEN" ""; Set-DotEnvValue "MESSENGER_PROVIDER" "none" }
  default { throw "MESSENGER_PROVIDER must be telegram, max or none" }
}
docker compose --profile telegram --profile max stop telegram-bot max-bot 2>$null | Out-Null

docker compose @ComposeProfile config --quiet
if ($LASTEXITCODE -ne 0) { throw "docker-compose.yml or .env validation failed" }
docker compose @ComposeProfile up -d --build --remove-orphans
if ($LASTEXITCODE -ne 0) { docker compose @ComposeProfile logs --tail=100; throw "docker compose failed" }
if (-not (Wait-Http "http://localhost:8000/api/health" 120)) { docker compose @ComposeProfile logs --tail=100 api; throw "API health check failed" }
if (-not (Wait-Http "http://localhost:5173" 120)) { docker compose @ComposeProfile logs --tail=100 web; throw "Web health check failed" }

Write-Host "`nZMK Vision installed and verified successfully." -ForegroundColor Green
Write-Host "Dashboard: http://localhost:5173"
Write-Host "API docs:  http://localhost:8000/docs"
Start-Process "http://localhost:5173"
if (-not $NonInteractive) { Read-Host "Press Enter to close" }
