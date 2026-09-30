# =====================================================================
# Zovod auto-updater (Windows).
#
# Normal mode:
#   & installers\auto-update.ps1 -Relaunch install-windows.ps1
#
# Channels (ZMK_UPDATE_CHANNEL):
#
#   commit (default)
#     Every commit — including a merge of any branch — counts as a new
#     version, exactly like the desktop updater of danilka-revin/linux_pcb_app:
#     the head commit of ZMK_UPDATE_BRANCH (default main) is compared with the
#     installed commit (COMMIT file / git HEAD) and the source archive of that
#     commit is unpacked, swapped into place (preserving .env and ./data) and
#     recorded in COMMIT / data\build-info.json.
#
#   release
#     Legacy behaviour: latest GitHub release, SHA256 checksum verification.
#
# Apply mode is invoked internally from the fresh staging tree, so the file
# being replaced is never the running script.
# =====================================================================
param(
  [string]$Relaunch = 'install-windows.ps1',
  [switch]$Apply,
  [string]$Staged,
  [string]$Root
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repo = if ($env:ZMK_REPO) { $env:ZMK_REPO } else { 'danilka-revin/zmk-videoanalytics' }
$channel = if ($env:ZMK_UPDATE_CHANNEL) { $env:ZMK_UPDATE_CHANNEL } else { 'commit' }
$branch = $env:ZMK_UPDATE_BRANCH
$apiUrl = if ($env:ZMK_API) { $env:ZMK_API } else { "https://api.github.com/repos/$repo/releases/latest" }
$dlBase = if ($env:ZMK_DL_BASE) { $env:ZMK_DL_BASE } else { "https://github.com/$repo/releases/download" }
$commitsApi = if ($env:ZMK_COMMITS_API) { $env:ZMK_COMMITS_API } else { "https://api.github.com/repos/$repo/commits" }
$codeloadBase = if ($env:ZMK_CODELOAD_BASE) { $env:ZMK_CODELOAD_BASE } else { "https://codeload.github.com/$repo/tar.gz" }
$rawBase = if ($env:ZMK_RAW_BASE) { $env:ZMK_RAW_BASE } else { "https://raw.githubusercontent.com/$repo" }
$shaPattern = '^[0-9a-fA-F]{40}$'

function Get-CurrentVersion([string]$dir) {
  $f = Join-Path $dir 'VERSION'
  if (Test-Path $f) { return ((Get-Content $f -Raw).Trim()) }
  return '0.0.0'
}

function Get-CurrentCommit([string]$dir) {
  $f = Join-Path $dir 'COMMIT'
  if (Test-Path $f) {
    $value = (Get-Content $f -Raw).Trim()
    if ($value -match $shaPattern) { return $value.ToLowerInvariant() }
  }
  $git = Get-Command git -ErrorAction SilentlyContinue
  if ($git -and (Test-Path (Join-Path $dir '.git'))) {
    try {
      $value = (& git -C $dir rev-parse HEAD 2>$null | Select-Object -First 1)
      if ($value -and $value.Trim() -match $shaPattern) { return $value.Trim().ToLowerInvariant() }
    } catch { }
  }
  return ''
}

function Get-Short([string]$sha) {
  if ($sha -and $sha -match $shaPattern) { return $sha.Substring(0, 7) }
  return ''
}

function Get-Branch([string]$dir) {
  if ($branch) { return $branch }
  if (Test-Path (Join-Path $dir '.git')) {
    try {
      $value = (& git -C $dir branch --show-current 2>$null | Select-Object -First 1)
      if ($value -and $value.Trim()) { return $value.Trim() }
    } catch { }
  }
  return 'main'
}

function Get-LatestCommit([string]$name) {
  $git = Get-Command git -ErrorAction SilentlyContinue
  if ($git) {
    try {
      $out = & git ls-remote --heads "https://github.com/$repo.git" "refs/heads/$name" 2>$null
      $first = ($out | Select-Object -First 1)
      if ($first) {
        $value = ($first -split '\s+')[0].Trim()
        if ($value -match $shaPattern) { return $value.ToLowerInvariant() }
      }
    } catch { }
  }
  $commit = Invoke-RestMethod -Uri "$commitsApi/$name" -Headers @{ Accept = 'application/vnd.github+json' } -TimeoutSec 20
  $value = [string]$commit.sha
  if ($value -match $shaPattern) { return $value.ToLowerInvariant() }
  throw "GitHub did not return the head commit of $name"
}

function Get-HeadVersion([string]$sha) {
  try {
    $value = (Invoke-WebRequest -Uri "$rawBase/$sha/VERSION" -UseBasicParsing -TimeoutSec 20).Content.Trim()
    if ($value -match '^v?\d+(\.\d+)*$') { return $value.TrimStart('v') }
  } catch { }
  return ''
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

# Record which build is installed: COMMIT + data\build-info.json (the API
# container mounts .\data and shows this version in the panel and logs).
function Write-BuildInfo([string]$dir, [string]$version, [string]$commit, [string]$name, [string]$chan) {
  $commit = if ($commit) { $commit.ToLowerInvariant() } else { '' }
  if ($commit -match $shaPattern) { Set-Content -Path (Join-Path $dir 'COMMIT') -Value $commit }
  $dataDir = Join-Path $dir 'data'
  New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
  $info = [ordered]@{
    version      = $version
    commit       = $commit
    short        = (Get-Short $commit)
    display      = if (Get-Short $commit) { "$version+$(Get-Short $commit)" } else { $version }
    branch       = $name
    channel      = $chan
    installed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  }
  ($info | ConvertTo-Json) + "`n" | Set-Content -Path (Join-Path $dataDir 'build-info.json') -Encoding UTF8
}

if ($Apply) {
  if (-not (Test-Path $Staged)) { throw "Staging directory missing: $Staged" }
  Write-Host "[auto-update] Applying update: $Staged -> $Root" -ForegroundColor Cyan
  # robocopy mirrors the tree, removing stale files while preserving
  # runtime data and secrets (data, .env, .zmk-profiles, databases).
  robocopy $Staged $Root /E /MIR /XD node_modules dist .git data /XF .env .zmk-profiles COMMIT *.db | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "Failed to copy update files (robocopy code $LASTEXITCODE)" }
  $version = Get-CurrentVersion $Staged
  $commit = if ($env:ZMK_APPLY_COMMIT) { $env:ZMK_APPLY_COMMIT } else { Get-CurrentCommit $Staged }
  $name = if ($env:ZMK_APPLY_BRANCH) { $env:ZMK_APPLY_BRANCH } else { if ($branch) { $branch } else { '' } }
  $chan = if ($env:ZMK_APPLY_CHANNEL) { $env:ZMK_APPLY_CHANNEL } else { $channel }
  Write-BuildInfo $Root $version $commit $name $chan
  Remove-Item -Recurse -Force (Split-Path $Staged -Parent) -ErrorAction SilentlyContinue
  $env:ZMK_RELAUNCHED_AFTER_UPDATE = '1'
  Write-Host "[auto-update] Version $version installed. Relaunching $Relaunch..." -ForegroundColor Green
  $target = Join-Path $Root "installers\$Relaunch"
  & $target
  exit $LASTEXITCODE
}

if ($env:ZMK_NO_AUTO_UPDATE -eq '1' -or $env:ZMK_RELAUNCHED_AFTER_UPDATE -eq '1') { return }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$cur = Get-CurrentVersion $root
$curCommit = Get-CurrentCommit $root

if ($channel -in @('release','releases','tag','tags')) {
  # --- legacy release channel: GitHub Releases + SHA256 ----------------------
  try {
    $rel = Invoke-RestMethod -Uri $apiUrl -Headers @{ Accept = 'application/vnd.github+json' } -TimeoutSec 20
    $tag = [string]$rel.tag_name
  } catch {
    Write-Host "[auto-update] Could not reach GitHub (offline or rate-limited); skipping update. Current: $cur" -ForegroundColor Yellow
    return
  }
  $ver = $tag.TrimStart('v')
  Write-Host "[auto-update] Channel: release  |  Current: $cur  |  Latest: $ver"
  if (-not (Test-VersionLt $cur $ver)) {
    Write-Host "[auto-update] Already up to date ($cur)."
    return
  }
  Write-Host "[auto-update] New version $ver detected. Downloading..." -ForegroundColor Cyan
  $base = "zmk-videoanalytics-$tag"
  $dl = "$dlBase/$tag"
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
}

# --- commit channel: every commit / merge of the branch is a new version ----
$name = Get-Branch $root
Write-Host "[auto-update] Channel: commit (branch $name)  |  Current: $cur$(if ($curCommit) { " ($($curCommit.Substring(0,7)))" })"
try {
  $latest = Get-LatestCommit $name
} catch {
  Write-Host "[auto-update] Could not reach GitHub (offline or rate-limited); skipping update check." -ForegroundColor Yellow
  return
}
if ($curCommit -and $curCommit -eq $latest) {
  Write-Host "[auto-update] Already up to date ($($curCommit.Substring(0,7)))."
  return
}
if (-not $curCommit) {
  $headVersion = Get-HeadVersion $latest
  if ($headVersion -and -not (Test-VersionLt $cur $headVersion)) {
    Write-Host "[auto-update] Installed build ($cur) is not older than $name ($headVersion); skipping."
    return
  }
}
# --- git checkout: move to the head commit, no archive download needed ------
if ((Test-Path (Join-Path $root '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
  & git -C $root fetch --prune --tags --force origin $name 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[auto-update] Could not fetch branch $name; skipping update." -ForegroundColor Yellow
    return
  }
  if ($curCommit -and $curCommit -eq $latest) {
    Write-Host "[auto-update] Already up to date ($($curCommit.Substring(0,7)))."
    return
  }
  $dirty = (& git -C $root status --porcelain --untracked-files=no 2>$null)
  if ($dirty) {
    Write-Host "[auto-update] Local changes detected; git update skipped." -ForegroundColor Yellow
    return
  }
  & git -C $root checkout -q -B $name $latest 2>$null
  if ($LASTEXITCODE -eq 0) {
    $checkedOut = Get-CurrentCommit $root
    Write-BuildInfo $root (Get-CurrentVersion $root) $checkedOut $name 'commit'
    $env:ZMK_RELAUNCHED_AFTER_UPDATE = '1'
    $shortOut = if ($checkedOut) { $checkedOut.Substring(0, 7) } else { '?' }
    Write-Host "[auto-update] Updated to $shortOut on $name. Relaunching $Relaunch..." -ForegroundColor Green
    $target = Join-Path $root "installers\$Relaunch"
    & $target
    exit $LASTEXITCODE
  }
  Write-Host "[auto-update] Git checkout of $($latest.Substring(0,7)) failed; falling back to the source archive." -ForegroundColor Yellow
}

Write-Host "[auto-update] New commit $($latest.Substring(0,7)) on $name. Downloading..." -ForegroundColor Cyan
$wd = Join-Path ([IO.Path]::GetTempPath()) ("zmk-update-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $wd | Out-Null
$tarball = Join-Path $wd "zmk-videoanalytics-$latest.tar.gz"
Invoke-WebRequest -Uri "$codeloadBase/$latest.tar.gz" -OutFile $tarball -UseBasicParsing
# Windows 10 (1803+) and Windows 11 ship bsdtar, which unpacks GitHub archives.
& tar -xzf $tarball -C $wd
if ($LASTEXITCODE -ne 0) { throw "Failed to extract $tarball" }
$staged = Get-ChildItem -Path $wd -Directory | Select-Object -First 1
if (-not $staged -or -not (Test-Path (Join-Path $staged.FullName 'VERSION'))) {
  throw "Archive of $($latest.Substring(0,7)) has no project directory"
}
# Run the freshly downloaded updater in apply mode (from staging), which
# performs the swap, records the commit and relaunches the target.
$env:ZMK_APPLY_COMMIT = $latest
$env:ZMK_APPLY_BRANCH = $name
$env:ZMK_APPLY_CHANNEL = 'commit'
& (Join-Path $staged.FullName 'installers\auto-update.ps1') -Apply -Relaunch $Relaunch -Staged $staged.FullName -Root $root
return
