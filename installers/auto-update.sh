#!/usr/bin/env bash
# =====================================================================
# Zovod auto-updater (Linux).
#
# Usage:
#   bash installers/auto-update.sh <relaunch-script>            # normal
#   bash installers/auto-update.sh <relaunch-script> apply <staged> <root>
#
# <relaunch-script> is the script to re-run after an update has been
# applied, e.g. "install-linux.sh" or "start.sh".
#
# Channels (ZMK_UPDATE_CHANNEL):
#
#   commit (default)
#     Every commit — including a merge of any branch — counts as a new
#     version, exactly like the desktop updater of danilka-revin/linux_pcb_app:
#       * a git checkout is fetched and moved to the head commit of the
#         tracked branch (ZMK_UPDATE_BRANCH, default: the current branch);
#       * an installation unpacked from an archive downloads the source
#         archive of that commit (codeload tarball), swaps it into place
#         preserving runtime data (.env, ./data, Docker volumes, databases)
#         and records the new commit in ./COMMIT and ./data/build-info.json.
#
#   release (ZMK_UPDATE_CHANNEL=release)
#     Legacy behaviour: query the latest GitHub release, download its
#     archive, verify the SHA256 checksum and swap it into place.
#
# In both channels an offline/failed check simply returns 0 so the caller
# can continue starting normally, and the "apply" mode runs from the freshly
# extracted staging directory, so overwriting files in place is always safe
# (the running script is never the file being replaced).
# =====================================================================
set -uo pipefail

# Override these via environment to point at a mirror (or for tests).
ZMK_REPO="${ZMK_REPO:-danilka-revin/zmk-videoanalytics}"
ZMK_REPO_URL="${ZMK_REPO_URL:-https://github.com/${ZMK_REPO}.git}"
ZMK_UPDATE_CHANNEL="${ZMK_UPDATE_CHANNEL:-commit}"
ZMK_UPDATE_BRANCH="${ZMK_UPDATE_BRANCH:-}"
# Release channel endpoints.
ZMK_API="${ZMK_API:-https://api.github.com/repos/${ZMK_REPO}/releases/latest}"
ZMK_DL_BASE="${ZMK_DL_BASE:-https://github.com/${ZMK_REPO}/releases/download}"
# Commit channel endpoints (head commit / source archive / VERSION of a commit).
# ZMK_UPDATE_NO_GIT=1 skips `git ls-remote` and uses the REST API only (a host
# without git access, or a mirror); it never changes what is installed.
ZMK_COMMITS_API="${ZMK_COMMITS_API:-https://api.github.com/repos/${ZMK_REPO}/commits}"
ZMK_CODELOAD_BASE="${ZMK_CODELOAD_BASE:-https://codeload.github.com/${ZMK_REPO}/tar.gz}"
ZMK_RAW_BASE="${ZMK_RAW_BASE:-https://raw.githubusercontent.com/${ZMK_REPO}}"

# Fix for "fatal: detected dubious ownership in repository" when running as root
# or via sudo on a directory owned by another user (e.g. /root/zmk-vision).
zmk_ensure_safe_git(){
  local dir="$1"
  [[ -n "$dir" ]] || return 0
  git config --global --add safe.directory "$dir" >/dev/null 2>&1 || true
  if command -v sudo >/dev/null 2>&1; then
    sudo git config --global --add safe.directory "$dir" >/dev/null 2>&1 || true
  fi
  if [[ -n "${SUDO_USER:-}" ]]; then
    sudo -u "$SUDO_USER" git config --global --add safe.directory "$dir" >/dev/null 2>&1 || true
  fi
}

# Remote git operations are capped: a stalled fetch used to hang the launcher
# with no output, which is indistinguishable from an endless download.
zmk_git(){
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=15s "${ZMK_GIT_TIMEOUT:-120}" git "$@"
  else
    git "$@"
  fi
}

zmk_err(){ echo "ERROR: $*" >&2; }
zmk_log(){ echo "[auto-update] $*"; }
zmk_short(){ printf '%s' "${1:0:7}"; }
zmk_label(){ printf '%s%s' "${1:-0.0.0}" "${2:+ ($(zmk_short "$2"))}"; }

zmk_current_version(){
  local root="$1" f version
  f="$root/VERSION"
  if [[ -f "$f" ]]; then
    version=$(tr -d '[:space:]' < "$f")
    [[ -n "$version" ]] && { echo "$version"; return 0; }
  fi
  echo "0.0.0"
}

# Installed commit: the ./COMMIT file written by the updater/installer, else
# git HEAD for a checkout.
zmk_current_commit(){
  local root="$1" f commit
  f="$root/COMMIT"
  if [[ -f "$f" ]]; then
    commit=$(tr -d '[:space:]' < "$f")
    [[ "$commit" =~ ^[0-9a-fA-F]{40}$ ]] && { printf '%s\n' "${commit,,}"; return 0; }
  fi
  if [[ -d "$root/.git" ]] && command -v git >/dev/null 2>&1; then
    commit=$(git -C "$root" rev-parse HEAD 2>/dev/null | tr -d '[:space:]')
    [[ "$commit" =~ ^[0-9a-fA-F]{40}$ ]] && { printf '%s\n' "${commit,,}"; return 0; }
  fi
  return 1
}

# Branch this installation follows: explicit ZMK_UPDATE_BRANCH, else the
# branch of the checkout, else main (archive installations follow main).
zmk_branch(){
  local root="$1" branch
  if [[ -n "$ZMK_UPDATE_BRANCH" ]]; then printf '%s\n' "$ZMK_UPDATE_BRANCH"; return 0; fi
  if [[ -d "$root/.git" ]]; then
    branch=$(git -C "$root" branch --show-current 2>/dev/null | tr -d '[:space:]')
    [[ -n "$branch" ]] && { printf '%s\n' "$branch"; return 0; }
  fi
  printf 'main\n'
}

# Head commit of a branch: git ls-remote first (no GitHub API quota), then
# the REST API.
zmk_latest_commit(){
  local branch="${1:-main}" sha json
  if [[ "${ZMK_UPDATE_NO_GIT:-0}" != "1" ]] && command -v git >/dev/null 2>&1; then
    sha=$(zmk_git ls-remote --heads "$ZMK_REPO_URL" "refs/heads/${branch}" 2>/dev/null \
      | awk 'NR==1{print $1}' | tr -d '[:space:]')
    if [[ "$sha" =~ ^[0-9a-fA-F]{40}$ ]]; then printf '%s\n' "${sha,,}"; return 0; fi
  fi
  if ! json=$(curl -fsSL --max-time 20 -H "Accept: application/vnd.github+json" "${ZMK_COMMITS_API}/${branch}" 2>/dev/null); then
    return 1
  fi
  sha=$(printf '%s' "$json" | grep -oE '"sha"[[:space:]]*:[[:space:]]*"[0-9a-fA-F]{40}"' | head -1 | grep -oE '[0-9a-fA-F]{40}')
  [[ "$sha" =~ ^[0-9a-fA-F]{40}$ ]] || return 1
  printf '%s\n' "${sha,,}"
}

# VERSION file of a commit (used when the installation has no recorded commit).
zmk_head_version(){
  local sha="$1" version
  version=$(curl -fsSL --max-time 20 "${ZMK_RAW_BASE}/${sha}/VERSION" 2>/dev/null | tr -d '[:space:]') || return 1
  [[ "$version" =~ ^v?[0-9]+(\.[0-9]+)*$ ]] || return 1
  printf '%s\n' "${version#v}"
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

# Record which build is installed: ./COMMIT + ./data/build-info.json (the API
# container mounts ./data and shows this version in the panel and logs).
zmk_write_build_info(){
  local root="$1" version="${2:-}" commit="${3:-}" branch="${4:-}" channel="${5:-commit}"
  [[ -n "$root" ]] || return 1
  commit="${commit,,}"
  if [[ "$commit" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' "$commit" > "$root/COMMIT" 2>/dev/null || true
  fi
  mkdir -p "$root/data" 2>/dev/null || return 1
  {
    printf '{\n'
    printf ' "version": "%s",\n' "${version}"
    printf ' "commit": "%s",\n' "${commit}"
    printf ' "short": "%s",\n' "$([[ "$commit" =~ ^[0-9a-f]{40}$ ]] && zmk_short "$commit")"
    printf ' "branch": "%s",\n' "${branch}"
    printf ' "channel": "%s",\n' "${channel}"
    printf ' "installed_at": "%s"\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '}\n'
  } > "$root/data/build-info.json" 2>/dev/null
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
  # remove files that no longer exist upstream (COMMIT is owned by the updater)
  ( cd "$dst" && find . -type f \
      ! -path './.git/*' ! -path './node_modules/*' ! -path './dist/*' \
      ! -path './data/*' ! -name '.env' ! -name '.zmk-profiles' ! -name 'COMMIT' ! -name '*.db' \
      | sed 's|^\./||' ) | while IFS= read -r rel; do
        if ! grep -qxF "$rel" "$newlist"; then rm -f "$dst/$rel"; fi
      done
  rm -f "$newlist"
}

zmk_apply_update(){
  local staged="$1" root="$2" relaunch="$3"
  local src="$staged"
  [[ -d "$src" ]] || { zmk_err "staging dir missing: $src"; exit 1; }
  zmk_log "Applying update: ${staged} -> ${root}"
  zmk_sync_tree "$src" "$root"
  # make sure the installers and launcher themselves are refreshed now
  if [[ -d "$src/installers" ]]; then
    ( cd "$src/installers" && tar cf - . ) | ( cd "$root/installers" && tar xf - )
  fi
  [[ -f "$src/start.sh" ]] && cp -f "$src/start.sh" "$root/start.sh" 2>/dev/null || true
  [[ -f "$src/start.ps1" ]] && cp -f "$src/start.ps1" "$root/start.ps1" 2>/dev/null || true
  # Which build was just installed? Set by the caller of apply; for the release
  # channel the archive may carry its own COMMIT file.
  local version commit branch channel
  version=$(tr -d '[:space:]' < "$src/VERSION" 2>/dev/null || true)
  commit="${ZMK_APPLY_COMMIT:-}"
  if [[ -z "$commit" && -f "$src/COMMIT" ]]; then
    commit=$(tr -d '[:space:]' < "$src/COMMIT")
  fi
  branch="${ZMK_APPLY_BRANCH:-${ZMK_UPDATE_BRANCH:-}}"
  channel="${ZMK_APPLY_CHANNEL:-${ZMK_UPDATE_CHANNEL:-commit}}"
  zmk_write_build_info "$root" "$version" "$commit" "$branch" "$channel" || true
  rm -rf "$src"
  zmk_log "Version ${version}${commit:+ ($(zmk_short "$commit"))} installed. Relaunching ${relaunch}..."
  exec env ZMK_RELAUNCHED_AFTER_UPDATE=1 bash "$root/${relaunch}"
}

# ---------------------------------------------------------------------
# Channel: commit — any commit / merge of the tracked branch is a version.
# ---------------------------------------------------------------------
zmk_check_commit(){
  local root="$1" relaunch="$2"
  local branch cur_version cur_commit latest head_version wd staged tarball
  branch=$(zmk_branch "$root")
  cur_version=$(zmk_current_version "$root")
  cur_commit=$(zmk_current_commit "$root" || true)
  [[ -d "$root/.git" ]] && zmk_ensure_safe_git "$root"
  zmk_log "Channel: commit (branch ${branch})  |  Current: $(zmk_label "$cur_version" "$cur_commit")"
  if ! latest=$(zmk_latest_commit "$branch"); then
    zmk_log "Could not determine the head commit of ${branch} (offline, rate-limited or no such branch); skipping update check."
    return 0
  fi

  # --- git checkout: move to the head commit, no archive download needed ---
  if [[ -d "$root/.git" ]]; then
    if ! zmk_git -C "$root" fetch --prune --tags --force origin "$branch" >/dev/null 2>&1; then
      zmk_err "Could not fetch branch ${branch}; skipping update."
      return 0
    fi
    if [[ -n "$cur_commit" && "$cur_commit" == "$latest" ]]; then
      zmk_log "Already up to date (${cur_commit:0:7})."
      return 0
    fi
    if ! git -C "$root" diff --quiet || ! git -C "$root" diff --cached --quiet; then
      zmk_log "Local changes detected; git update skipped."
      return 0
    fi
    if zmk_git -C "$root" checkout -q -B "$branch" "$latest" 2>/dev/null \
      || zmk_git -C "$root" checkout -q -B "$branch" "origin/${branch}" 2>/dev/null; then
      local checked_out
      checked_out=$(zmk_current_commit "$root" || true)
      zmk_write_build_info "$root" "$(zmk_current_version "$root")" "$checked_out" "$branch" commit || true
      zmk_log "Updated to ${checked_out:0:7} on ${branch}. Relaunching ${relaunch}..."
      exec env ZMK_RELAUNCHED_AFTER_UPDATE=1 bash "$root/${relaunch}"
    fi
    zmk_err "Git checkout of ${latest:0:7} failed."
    return 0
  fi

  # --- archive installation: download the source of the branch head --------
  if [[ -n "$cur_commit" && "$cur_commit" == "$latest" ]]; then
    zmk_log "Already up to date (${cur_commit:0:7})."
    return 0
  fi
  if [[ -z "$cur_commit" ]]; then
    head_version=$(zmk_head_version "$latest" || true)
    if [[ -n "$head_version" ]] && ! zmk_version_lt "$cur_version" "$head_version"; then
      zmk_log "Installed build (${cur_version}) is not older than ${branch} (${head_version}); skipping."
      return 0
    fi
  fi
  zmk_log "New commit ${latest:0:7} on ${branch}. Downloading..."
  wd=$(mktemp -d) || { zmk_err "cannot create temp dir"; return 1; }
  tarball="$wd/zmk-videoanalytics-${latest}.tar.gz"
  if ! curl -fsSL --retry 3 --retry-delay 2 --max-time 900 -A zmk-updater \
      -o "$tarball" "${ZMK_CODELOAD_BASE}/${latest}.tar.gz"; then
    zmk_err "download failed: ${ZMK_CODELOAD_BASE}/${latest}.tar.gz"
    rm -rf "$wd"
    return 1
  fi
  if ! tar -xzf "$tarball" -C "$wd"; then
    zmk_err "failed to extract ${tarball}"
    rm -rf "$wd"
    return 1
  fi
  staged=$(find "$wd" -mindepth 1 -maxdepth 1 -type d | head -1)
  if [[ -z "$staged" || ! -f "$staged/VERSION" ]]; then
    zmk_err "archive of ${latest:0:7} has no project directory"
    rm -rf "$wd"
    return 1
  fi
  # Apply from the fresh staging tree, which records the commit it was built from.
  exec env ZMK_RELAUNCHED_AFTER_UPDATE=1 \
    ZMK_APPLY_COMMIT="$latest" \
    ZMK_APPLY_BRANCH="$branch" \
    ZMK_APPLY_CHANNEL=commit \
    bash "$staged/installers/auto-update.sh" apply "$relaunch" "$staged" "$root"
}

# ---------------------------------------------------------------------
# Channel: release — legacy GitHub Releases + SHA256 verification.
# ---------------------------------------------------------------------
zmk_check_release(){
  local root="$1" relaunch="$2"
  local cur latest wd
  if [[ -d "$root/.git" ]]; then zmk_ensure_safe_git "$root"; fi
  cur=$(zmk_current_version "$root")
  if ! latest=$(zmk_latest_version); then
    zmk_log "Could not reach GitHub (offline or rate-limited); skipping update check. Current version: ${cur}."
    return 0
  fi
  local latest_plain
  latest_plain="${latest#v}"
  zmk_log "Channel: release  |  Current: ${cur}  |  Latest: ${latest_plain}"
  if ! zmk_version_lt "$cur" "$latest_plain"; then
    zmk_log "Already up to date (${cur})."
    return 0
  fi
  zmk_log "New version ${latest_plain} detected. Downloading..."
  wd=$(mktemp -d) || { zmk_err "cannot create temp dir"; return 1; }
  local base="zmk-videoanalytics-${latest}"
  local dl="${ZMK_DL_BASE}/${latest}"
  local tarball="${base}.tar.gz"
  local dl_ok=0
  if curl -fsSL --retry 3 --retry-delay 2 --max-time 900 -o "$wd/$tarball" "$dl/$tarball"; then
    if curl -fsSL --retry 3 --max-time 60 -o "$wd/SHA256SUMS.txt" "$dl/SHA256SUMS.txt"; then
      local expected actual
      expected=$(awk -v f="$tarball" '$2==f{print $1}' "$wd/SHA256SUMS.txt")
      if [[ -n "$expected" ]]; then
        actual=$(sha256sum "$wd/$tarball" | awk '{print $1}')
        if [[ "$expected" == "$actual" ]]; then
          zmk_log "SHA256 verified."
          dl_ok=1
        else
          zmk_err "SHA256 mismatch for ${tarball} (expected ${expected}, got ${actual})"
        fi
      else
        zmk_err "no checksum for ${tarball} in SHA256SUMS.txt"
      fi
    else
      zmk_err "could not fetch SHA256SUMS.txt from $dl"
    fi
  else
    zmk_err "download failed: ${dl}/${tarball} (will try git fallback)"
  fi
  if [[ "$dl_ok" == "1" ]]; then
    if ! tar -xzf "$wd/$tarball" -C "$wd"; then
      zmk_err "failed to extract ${tarball}"; rm -rf "$wd"; return 1
    fi
    local staged="$wd/zmk-videoanalytics"
    [[ -d "$staged" ]] || { zmk_err "archive has no zmk-videoanalytics directory"; rm -rf "$wd"; return 1; }
    # Run the NEW updater from the staging tree in apply mode
    exec env ZMK_RELAUNCHED_AFTER_UPDATE=1 ZMK_APPLY_CHANNEL=release \
      bash "$staged/installers/auto-update.sh" apply "${relaunch}" "$staged" "$root"
  fi
  # Fallback: git-based update if tarball not available (e.g. 404 before Release assets uploaded)
  if [[ -d "$root/.git" ]]; then
    zmk_log "Tarball unavailable, trying git fetch for ${latest}..."
    # Fix dubious ownership + divergent branches + local changes
    zmk_ensure_safe_git "$root"
    git -C "$root" config --global --add safe.directory "$root" >/dev/null 2>&1 || true
    git -C "$root" config pull.rebase false >/dev/null 2>&1 || true
    git -C "$root" config pull.ff only >/dev/null 2>&1 || true
    # Stash or discard local changes that would block checkout (VERSION, RELEASE_NOTES, Dockerfiles)
    git -C "$root" reset --hard HEAD >/dev/null 2>&1 || true
    git -C "$root" clean -fd >/dev/null 2>&1 || true
    zmk_git -C "$root" fetch --prune --tags --force origin 2>&1 | tail -5 || true
    # Try to checkout the tag directly, then main if tag checkout fails
    if zmk_git -C "$root" fetch --depth=1 origin "$latest" 2>&1 || zmk_git -C "$root" fetch origin "$latest" --prune --tags 2>&1 | tail -5; then
      # Ensure clean state again before checkout
      git -C "$root" reset --hard HEAD >/dev/null 2>&1 || true
      git -C "$root" clean -fd >/dev/null 2>&1 || true
      if git -C "$root" checkout -B main FETCH_HEAD 2>&1 || git -C "$root" checkout -B main "origin/main" 2>&1 || git -C "$root" checkout "$latest" 2>&1 || git -C "$root" checkout -B main "origin/$latest" 2>&1; then
        zmk_log "Git update to ${latest} succeeded, relaunching ${relaunch}..."
        rm -rf "$wd"
        exec env ZMK_RELAUNCHED_AFTER_UPDATE=1 bash "$root/${relaunch}"
      fi
    fi
    # Last resort: fetch main
    zmk_ensure_safe_git "$root"
    git -C "$root" reset --hard HEAD >/dev/null 2>&1 || true
    git -C "$root" clean -fd >/dev/null 2>&1 || true
    if zmk_git -C "$root" fetch --depth=1 origin main 2>&1 || zmk_git -C "$root" fetch origin main --prune --tags 2>&1 | tail -5; then
      git -C "$root" reset --hard HEAD >/dev/null 2>&1 || true
      git -C "$root" clean -fd >/dev/null 2>&1 || true
      if git -C "$root" checkout -B main FETCH_HEAD 2>&1 || git -C "$root" checkout -B main origin/main 2>&1; then
        zmk_log "Git update to main succeeded, relaunching ${relaunch}..."
        rm -rf "$wd"
        exec env ZMK_RELAUNCHED_AFTER_UPDATE=1 bash "$root/${relaunch}"
      fi
    fi
    zmk_err "git fallback also failed"
  fi
  rm -rf "$wd"
  return 1
}

zmk_check_and_update(){
  local root="$1" relaunch="$2"
  case "${ZMK_UPDATE_CHANNEL,,}" in
    release|releases|tag|tags) zmk_check_release "$root" "$relaunch" ;;
    *)                          zmk_check_commit  "$root" "$relaunch" ;;
  esac
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
