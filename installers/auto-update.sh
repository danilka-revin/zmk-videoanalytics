#!/usr/bin/env bash
# =====================================================================
# ZMK Vision auto-updater (Linux).
#
# Usage:
#   bash installers/auto-update.sh <relaunch-script>            # normal
#   bash installers/auto-update.sh <relaunch-script> apply <staged> <root>
#
# <relaunch-script> is the script to re-run after an update has been
# applied, e.g. "install-linux.sh" or "start.sh".
#
# Behaviour:
#   * Reads the current version from ./VERSION.
#   * Queries the latest GitHub release of danilka-revin/zmk-videoanalytics.
#   * If a newer release exists: downloads the archive, verifies the
#     SHA256 checksum, extracts it and swaps it into place, preserving
#     runtime data (.env, ./data, Docker named volumes), then re-launches
#     the update target.
#   * If no new release (or network is unavailable) it simply returns 0
#     so the caller can continue starting normally.
#
# The "apply" mode runs from the freshly extracted staging directory, so
# overwriting files in place is always safe (the running script is never
# the file being replaced).
# =====================================================================
set -uo pipefail

# Override these via environment to point at a mirror (or for tests).
ZMK_REPO="${ZMK_REPO:-danilka-revin/zmk-videoanalytics}"
ZMK_API="${ZMK_API:-https://api.github.com/repos/${ZMK_REPO}/releases/latest}"
ZMK_DL_BASE="${ZMK_DL_BASE:-https://github.com/${ZMK_REPO}/releases/download}"

zmk_err(){ echo "ERROR: $*" >&2; }

zmk_current_version(){
  local root="$1" f version
  f="$root/VERSION"
  if [[ -f "$f" ]]; then
    version=$(tr -d '[:space:]' < "$f")
    [[ -n "$version" ]] && { echo "$version"; return 0; }
  fi
  echo "0.0.0"
}

zmk_latest_version(){
  local json tag
  if ! json=$(curl -fsSL --max-time 20 -H "Accept: application/vnd.github+json" "$ZMK_API" 2>/dev/null); then
    return 1
  fi
  tag=$(printf '%s' "$json" | grep -oE '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')
  [[ -z "$tag" ]] && return 1
  echo "$tag"
}

# zmk_version_lt <a> <b>: returns 0 when a < b (numeric semver compare).
zmk_version_lt(){
  local a b
  a=$(printf '%s' "$1" | sed -E 's/[^0-9.].*$//')
  b=$(printf '%s' "$2" | sed -E 's/[^0-9.].*$//')
  [[ "$a" == "$b" ]] && return 1
  local ai=() bi=() i x y
  IFS='.' read -r -a ai <<< "$a"
  IFS='.' read -r -a bi <<< "$b"
  for i in 0 1 2; do
    x="${ai[$i]:-0}"; y="${bi[$i]:-0}"
    if (( x < y )); then return 0; fi
    if (( x > y )); then return 1; fi
  done
  return 1
}

# cmpreq: copy a file tree onto another, overwriting, and removing stale files.
zmk_sync_tree(){
  local src="$1" dst="$2" newlist workdir rel
  newlist=$(mktemp)
  # build protected-path pattern
  ( cd "$src" && find . -type f ! -path './node_modules/*' ! -path './dist/*' | sed 's|^\./||' ) > "$newlist"
  # copy new over old (never touch runtime data / secrets)
  ( cd "$src" && tar --exclude='./.git' --exclude='./node_modules' --exclude='./dist' \
      --exclude='./.env' --exclude='./data' --exclude='./.zmk-profiles' \
      --exclude='./videoanalytics.db' --exclude='./*.db' -cf - . ) | ( cd "$dst" && tar -xf - )
  # remove files that no longer exist upstream
  ( cd "$dst" && find . -type f \
      ! -path './.git/*' ! -path './node_modules/*' ! -path './dist/*' \
      ! -path './data/*' ! -name '.env' ! -name '.zmk-profiles' ! -name '*.db' \
      | sed 's|^\./||' ) | while IFS= read -r rel; do
        if ! grep -qxF "$rel" "$newlist"; then rm -f "$dst/$rel"; fi
      done
  rm -f "$newlist"
}

zmk_apply_update(){
  local staged="$1" root="$2" relaunch="$3"
  local src="$staged"
  [[ -d "$src" ]] || { zmk_err "staging dir missing: $src"; exit 1; }
  echo "[auto-update] Applying update: ${staged} -> ${root}"
  zmk_sync_tree "$src" "$root"
  # make sure the installers and launcher themselves are refreshed now
  if [[ -d "$src/installers" ]]; then
    ( cd "$src/installers" && tar cf - . ) | ( cd "$root/installers" && tar xf - )
  fi
  [[ -f "$src/start.sh" ]] && cp -f "$src/start.sh" "$root/start.sh" 2>/dev/null || true
  [[ -f "$src/start.ps1" ]] && cp -f "$src/start.ps1" "$root/start.ps1" 2>/dev/null || true
  rm -rf "$src"
  echo "[auto-update] Update installed. Relaunching ${relaunch}..."
  exec env ZMK_RELAUNCHED_AFTER_UPDATE=1 bash "$root/${relaunch}" 
}

zmk_check_and_update(){
  local root="$1" relaunch="$2"
  local cur latest wd
  cur=$(zmk_current_version "$root")
  if ! latest=$(zmk_latest_version); then
    echo "[auto-update] Could not reach GitHub (offline or rate-limited); skipping update check. Current version: ${cur}."
    return 0
  fi
  local latest_plain
  latest_plain="${latest#v}"
  echo "[auto-update] Current: ${cur}  |  Latest: ${latest_plain}"
  if ! zmk_version_lt "$cur" "$latest_plain"; then
    echo "[auto-update] Already up to date (${cur})."
    return 0
  fi
  echo "[auto-update] New version ${latest_plain} detected. Downloading..."
  wd=$(mktemp -d) || { zmk_err "cannot create temp dir"; return 1; }
  local base="zmk-videoanalytics-${latest}"
  local dl="${ZMK_DL_BASE}/${latest}"
  local tarball="${base}.tar.gz"
  if ! curl -fsSL --retry 3 --retry-delay 2 --max-time 900 -o "$wd/$tarball" "$dl/$tarball"; then
    zmk_err "download failed: ${dl}/${tarball}"; rm -rf "$wd"; return 1
  fi
  if ! curl -fsSL --retry 3 --max-time 60 -o "$wd/SHA256SUMS.txt" "$dl/SHA256SUMS.txt"; then
    zmk_err "could not fetch SHA256SUMS.txt"; rm -rf "$wd"; return 1
  fi
  local expected actual
  expected=$(awk -v f="$tarball" '$2==f{print $1}' "$wd/SHA256SUMS.txt")
  if [[ -z "$expected" ]]; then
    zmk_err "no checksum for ${tarball} in SHA256SUMS.txt"; rm -rf "$wd"; return 1
  fi
  actual=$(sha256sum "$wd/$tarball" | awk '{print $1}')
  if [[ "$expected" != "$actual" ]]; then
    zmk_err "SHA256 mismatch for ${tarball} (expected ${expected}, got ${actual})"; rm -rf "$wd"; return 1
  fi
  echo "[auto-update] SHA256 verified."
  if ! tar -xzf "$wd/$tarball" -C "$wd"; then
    zmk_err "failed to extract ${tarball}"; rm -rf "$wd"; return 1
  fi
  local staged="$wd/zmk-videoanalytics"
  [[ -d "$staged" ]] || { zmk_err "archive has no zmk-videoanalytics directory"; rm -rf "$wd"; return 1; }
  # Run the NEW updater from the staging tree in apply mode; it swaps files
  # into $root, then relaunches $relaunch. Reading from staging (not $root)
  # keeps the currently running script safe while files are replaced.
  exec env ZMK_RELAUNCHED_AFTER_UPDATE=1 bash "$staged/installers/auto-update.sh" apply "${relaunch}" "$staged" "$root"
}

# Only run the auto-update flow when this file is executed directly
# (not when it is sourced for tests).
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-}" in
    apply)
      # args: apply <relaunch> <staged> <root>
      zmk_apply_update "$3" "$4" "$2"
      ;;
    *)
      RELAUNCH="${1:-install-linux.sh}"
      ROOT="${ZMK_INSTALL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
      if [[ "${ZMK_NO_AUTO_UPDATE:-}" == "1" || "${ZMK_RELAUNCHED_AFTER_UPDATE:-}" == "1" ]]; then
        exit 0
      fi
      zmk_check_and_update "$ROOT" "$RELAUNCH"
      ;;
  esac
fi
