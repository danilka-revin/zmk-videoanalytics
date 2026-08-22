# =====================================================================
# ZMK Vision auto-updater (Windows).
#
# Normal mode:
#   & installers\auto-update.ps1 -Relaunch install-windows.ps1
#
# Checks the latest GitHub release of danilka-revin/zmk-videoanalytics.
# If a newer build exists it downloads the zip, verifies the SHA256
# checksum, unpacks it, swaps it into place (preserving .env and ./data)
# and relaunches the update target. If there is no newer release, it
# simply exits 0 so the caller can continue.
#
# Apply mode is invoked internally from the fresh staging tree, so the
# file being replaced is never the running script.
# =====================================================================
param(
  [string]$Relaunch = 'install-windows.ps1',
  [switch]$Apply,
  [string]$Staged,
  [string]$Root
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repo = 'danilka-revin/zmk-videoanalytics'
$apiUrl = "https://api.github.com/repos/$repo/releases/latest"

function Get-CurrentVersion([string]$dir) {
  $f = Join-Path $dir 'VERSION'
  if (Test-Path $f) { return ((Get-Content $f -Raw).Trim()) }
  return '0.0.0'
}

function Test-VersionLt([string]$a, [string]$b) {
  $an = ($a -replace '[^\d.]','').Split('.')
  $bn = ($b -replace '[^\d.]','').Split('.')
  for ($i = 0; $i -lt 3; $i++) {
    $x = if ($an.Length -gt $i) { [int]$an[$i] } else { 0 }
    $y = if ($bn.Length -gt $i) { [int]$bn[$i] } else { 0 }
    if ($x -lt $y) { return $true }
    if ($x -gt $y) { return $false }
  }
  return $false
}

if ($Apply) {
  if (-not (Test-Path $Staged)) { throw "Staging directory missing: $Staged" }
  Write-Host "[auto-update] Applying update: $Staged -> $Root" -ForegroundColor Cyan
  # robocopy mirrors the tree, removing stale files while preserving
  # runtime data and secrets (data, .env, .zmk-profiles, databases).
  robocopy $Staged $Root /E /MIR /XD node_modules dist .git data /XF .env .zmk-profiles *.db | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "Failed to copy update files (robocopy code $LASTEXITCODE)" }
  Remove-Item -Recurse -Force (Split-Path $Staged -Parent) -ErrorAction SilentlyContinue
  $env:ZMK_RELAUNCHED_AFTER_UPDATE = '1'
  Write-Host "[auto-update] Update installed. Relaunching $Relaunch..." -ForegroundColor Green
  $target = Join-Path $Root "installers\$Relaunch"
  & $target
  exit $LASTEXITCODE
}

if ($env:ZMK_NO_AUTO_UPDATE -eq '1' -or $env:ZMK_RELAUNCHED_AFTER_UPDATE -eq '1') { return }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$cur = Get-CurrentVersion $root
try {
  $rel = Invoke-RestMethod -Uri $apiUrl -Headers @{ Accept = 'application/vnd.github+json' } -TimeoutSec 20
  $tag = [string]$rel.tag_name
} catch {
  Write-Host "[auto-update] Could not reach GitHub (offline or rate-limited); skipping update. Current: $cur" -ForegroundColor Yellow
  return
}
$ver = $tag.TrimStart('v')
Write-Host "[auto-update] Current: $cur  |  Latest: $ver"
if (-not (Test-VersionLt $cur $ver)) {
  Write-Host "[auto-update] Already up to date ($cur)."
  return
}
Write-Host "[auto-update] New version $ver detected. Downloading..." -ForegroundColor Cyan
$base = "zmk-videoanalytics-$tag"
$dl = "https://github.com/$repo/releases/download/$tag"
$wd = Join-Path ([IO.Path]::GetTempPath()) ("zmk-update-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $wd | Out-Null
$zip = Join-Path $wd "$base.zip"
Invoke-WebRequest -Uri "$dl/$base.zip" -OutFile $zip -UseBasicParsing
Invoke-WebRequest -Uri "$dl/SHA256SUMS.txt" -OutFile (Join-Path $wd 'SHA256SUMS.txt') -UseBasicParsing
$expected = ((Get-Content (Join-Path $wd 'SHA256SUMS.txt')) | Where-Object { $_ -match [regex]::Escape("$base.zip") } | Select-Object -First 1) -split '\s+' | Select-Object -First 1
if (-not $expected) { throw "No checksum entry for $base.zip in SHA256SUMS.txt" }
$actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($expected.ToLower() -ne $actual.ToLower()) { throw "SHA256 mismatch: expected $expected, got $actual" }
Write-Host "[auto-update] SHA256 verified."
Expand-Archive -Path $zip -DestinationPath $wd -Force
$staged = Join-Path $wd 'zmk-videoanalytics'
if (-not (Test-Path $staged)) { throw "Archive has no zmk-videoanalytics directory" }
# Run the freshly downloaded updater in apply mode (from staging), which
# performs the swap and relaunches the target.
& (Join-Path $staged 'installers\auto-update.ps1') -Apply -Relaunch $Relaunch -Staged $staged -Root $root
return
