# =====================================================================
# Zovod launcher (Windows).
#
# On every start this launcher:
#   1. Checks the head commit of the tracked branch (any commit or merge is a
#      new version; ZMK_UPDATE_CHANNEL=release restores release-only updates).
#   2. If it differs from the installed commit (COMMIT / git HEAD): downloads
#      the source of that commit, swaps it in and re-launches this script.
#   3. Otherwise (or after an update) it starts the stack with Docker.
#
# Set $env:ZMK_NO_AUTO_UPDATE='1' to skip the version check (offline).
# =====================================================================
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path (Join-Path $Root 'installers\auto-update.ps1'))) { throw "Missing installers\auto-update.ps1 — run install-windows.ps1 first." }
# The updater is commit-based: a Git checkout is moved to the head commit of
# the branch it currently tracks, so a feature branch is no longer replaced by
# a release archive — it simply follows its own branch.
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

# Record the running build (VERSION + commit) in COMMIT / data\build-info.json.
# Commit-based updates make the git commit the identity of a build — every
# commit or merge of the tracked branch counts as a new version — and the API
# container (which mounts .\data) reads this file for the panel and logs.
function Write-ZmkBuildInfo([string]$Dir) {
  if (-not $Dir) { $Dir = (Get-Location).Path }
  $version = ''
  if (Test-Path (Join-Path $Dir 'VERSION')) { $version = (Get-Content (Join-Path $Dir 'VERSION') -Raw).Trim() }
  $commit = ''
  if (Test-Path (Join-Path $Dir 'COMMIT')) { $commit = (Get-Content (Join-Path $Dir 'COMMIT') -Raw).Trim() }
  $git = Get-Command git -ErrorAction SilentlyContinue
  if ((-not ($commit -match '^[0-9a-fA-F]{40}$')) -and $git -and (Test-Path (Join-Path $Dir '.git'))) {
    try { $commit = (& git -C $Dir rev-parse HEAD 2>$null | Select-Object -First 1).Trim() } catch { }
  }
  if (-not ($commit -match '^[0-9a-fA-F]{40}$')) { $commit = '' } else { $commit = $commit.ToLowerInvariant() }
  $branch = if ($env:ZMK_UPDATE_BRANCH) { $env:ZMK_UPDATE_BRANCH } else { '' }
  if (-not $branch -and $git -and (Test-Path (Join-Path $Dir '.git'))) {
    try { $branch = (& git -C $Dir branch --show-current 2>$null | Select-Object -First 1).Trim() } catch { }
  }
  $channel = if ($env:ZMK_UPDATE_CHANNEL) { $env:ZMK_UPDATE_CHANNEL } else { 'commit' }
  $short = if ($commit) { $commit.Substring(0, 7) } else { '' }
  if ($commit) { Set-Content -Path (Join-Path $Dir 'COMMIT') -Value $commit }
  $dataDir = Join-Path $Dir 'data'
  New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
  # Nothing changed since the last launch: keep installed_at meaningful.
  $infoPath = Join-Path $dataDir 'build-info.json'
  if (Test-Path $infoPath) {
    try {
      $existing = Get-Content $infoPath -Raw | ConvertFrom-Json
      if ($existing.commit -eq $commit -and $existing.version -eq $version) { return }
    } catch { }
  }
  $info = [ordered]@{
    version      = $version
    commit       = $commit
    short        = $short
    branch       = $branch
    channel      = $channel
    installed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  }
  ($info | ConvertTo-Json) + "`n" | Set-Content -Path $infoPath -Encoding UTF8
}

Write-Host '[start] Starting Zovod services...'
docker compose @ComposeProfile config --quiet
if ($LASTEXITCODE -ne 0) { throw 'docker-compose.yml or .env validation failed' }
Write-ZmkBuildInfo $Root
docker compose @ComposeProfile up -d --build --remove-orphans
if ($LASTEXITCODE -ne 0) { docker compose @ComposeProfile logs --tail=100; throw 'docker compose failed' }
if (-not (Wait-Http 'http://localhost:8000/api/health' 120)) { docker compose logs --tail=100 api; throw 'API health check failed' }
if (-not (Wait-Http 'http://localhost:5173' 120)) { docker compose logs --tail=100 web; throw 'Web health check failed' }

Write-Host 'Zovod is running.' -ForegroundColor Green
Write-Host 'Dashboard: http://localhost:5173'
Write-Host 'API docs:  http://localhost:8000/docs'
Start-Process 'http://localhost:5173'
