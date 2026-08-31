#!/usr/bin/env bash
# =====================================================================
# ZMK Vision bootstrap / update launcher for Linux.
#
# One command installs or updates a clean Git checkout, preserving .env,
# data and Docker volumes on later runs, then starts the project.
#
# Default:
#   curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh | bash
#
# Optional environment:
#   ZMK_REF=main                 Git ref to install/update
#   ZMK_INSTALL_DIR=~/zmk-vision install location
#   ZMK_REPO=owner/repository    GitHub repository
# =====================================================================
set -Eeuo pipefail

ZMK_REPO="${ZMK_REPO:-danilka-revin/zmk-videoanalytics}"
ZMK_INSTALL_DIR="${ZMK_INSTALL_DIR:-$HOME/zmk-vision}"
ZMK_GIT_URL="https://github.com/${ZMK_REPO}.git"
ZMK_BIN_DIR="${HOME}/.local/bin"
ZMK_REF="${ZMK_REF:-}"

if [[ "${1:-}" == "--check" ]]; then
  echo "ZMK bootstrap launcher: OK"
  exit 0
fi

# --- helpers must be defined before first use (fix v2.15.1) ---
fail(){ echo "ERROR: $*" >&2; exit 1; }
info(){ echo "[zmk-bootstrap] $*"; }

run_privileged(){
  if [[ "${EUID}" -eq 0 ]]; then "$@"; else command -v sudo >/dev/null 2>&1 || fail "sudo is required to install missing dependencies"; sudo "$@"; fi
}

ensure_command(){
  local command_name="$1" package_name="$2"
  command -v "$command_name" >/dev/null 2>&1 && return 0
  command -v apt-get >/dev/null 2>&1 || fail "Missing ${command_name}. Install it manually, then run this command again."
  info "Installing ${package_name}..."
  run_privileged apt-get update
  run_privileged apt-get install -y "$package_name"
}

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

ensure_command git git
ensure_command curl curl

# Resolve the update ref:
#   1) an explicit launcher/env override wins;
#   2) a legacy launcher that pins ZMK_REF=main but currently sits on a work
#      branch is migrated to that branch once (so the previous one-command
#      launcher does not silently revert to the release channel);
#   3) otherwise the pinned ref recorded in .zmk-ref (set by a previous run);
#   4) otherwise the public release channel (main).
# The launcher stores ZMK_REF=auto so a one-command `zmk-vision` keeps tracking
# the current work branch until it disappears after the PR is merged, and then
# automatically falls back to main.
CURRENT_BRANCH=""
if [[ -d "$ZMK_INSTALL_DIR/.git" ]]; then
  zmk_ensure_safe_git "$ZMK_INSTALL_DIR"
  CURRENT_BRANCH="$(git -C "$ZMK_INSTALL_DIR" branch --show-current 2>/dev/null | tr -d '[:space:]' || true)"
fi
LEGACY_LAUNCHER="$ZMK_BIN_DIR/zmk-vision"
if [[ "${ZMK_REF:-}" == "main" && -x "$LEGACY_LAUNCHER" && "$CURRENT_BRANCH" != "main" && -n "$CURRENT_BRANCH" ]]; then
  if grep -qE 'exec env ZMK_REF=main ' "$LEGACY_LAUNCHER" 2>/dev/null; then
    echo "[zmk-bootstrap] Migrating legacy launcher to the current work branch: ${CURRENT_BRANCH}"
    ZMK_REF="$CURRENT_BRANCH"
  fi
fi
if [[ "$ZMK_REF" == "auto" || -z "$ZMK_REF" ]]; then
  if [[ -f "$ZMK_INSTALL_DIR/.zmk-ref" ]]; then
    ZMK_REF="$(tr -d '[:space:]' < "$ZMK_INSTALL_DIR/.zmk-ref")"
  else
    ZMK_REF="main"
  fi
fi

if [[ -d "$ZMK_INSTALL_DIR/.git" ]]; then
  zmk_ensure_safe_git "$ZMK_INSTALL_DIR"
  info "Existing installation found: $ZMK_INSTALL_DIR"
  current_origin=$(git -C "$ZMK_INSTALL_DIR" remote get-url origin 2>/dev/null || true)
  [[ "$current_origin" == "$ZMK_GIT_URL" ]] || fail "The target has another git origin: ${current_origin}"
  changes=$(git -C "$ZMK_INSTALL_DIR" status --porcelain --untracked-files=no)
  [[ -z "$changes" ]] || fail "Tracked local changes found. They were not overwritten; commit or stash them first."

  # A pinned feature branch that no longer exists on the remote has normally
  # been merged and deleted. Fall back to the public release channel so the
  # one-command launcher keeps working without manual maintenance.
  if [[ "$ZMK_REF" != "main" ]]; then
    if ! git ls-remote --heads "$ZMK_GIT_URL" "$ZMK_REF" >/dev/null 2>&1; then
      info "Pinned ref ${ZMK_REF} is gone (probably merged); switching to main."
      ZMK_REF="main"
    fi
  fi

  info "Downloading ${ZMK_REF} from GitHub..."
  # Fix dubious ownership + divergent branches + local changes that block checkout
  zmk_ensure_safe_git "$ZMK_INSTALL_DIR"
  git -C "$ZMK_INSTALL_DIR" config --global --add safe.directory "$ZMK_INSTALL_DIR" >/dev/null 2>&1 || true
  git -C "$ZMK_INSTALL_DIR" config pull.rebase false >/dev/null 2>&1 || true
  git -C "$ZMK_INSTALL_DIR" config pull.ff only >/dev/null 2>&1 || true
  git -C "$ZMK_INSTALL_DIR" reset --hard HEAD >/dev/null 2>&1 || true
  git -C "$ZMK_INSTALL_DIR" clean -fd >/dev/null 2>&1 || true
  git -C "$ZMK_INSTALL_DIR" fetch --prune --tags --force origin 2>&1 | tail -5 || true
  # An explicit `git fetch origin <branch>` records the commit in FETCH_HEAD
  # but does not necessarily create origin/<branch> in shallow checkouts.
  # Checking out FETCH_HEAD works for main as well as slash-containing feature
  # branches (e.g. arena/...) and keeps the one-command launcher repeatable.
  # Use --depth=1 for speed, fallback to full fetch if shallow fails
  if ! git -C "$ZMK_INSTALL_DIR" fetch --depth=1 origin "$ZMK_REF" 2>&1; then
    git -C "$ZMK_INSTALL_DIR" fetch origin "$ZMK_REF" --prune --tags 2>&1 | tail -5 || true
  fi
  git -C "$ZMK_INSTALL_DIR" reset --hard HEAD >/dev/null 2>&1 || true
  git -C "$ZMK_INSTALL_DIR" clean -fd >/dev/null 2>&1 || true
  # Try tag first, then branch - handle both v2.14.0 tag and main branch
  git -C "$ZMK_INSTALL_DIR" checkout -B "$ZMK_REF" FETCH_HEAD 2>&1 || git -C "$ZMK_INSTALL_DIR" checkout -B main origin/main 2>&1 || git -C "$ZMK_INSTALL_DIR" checkout -B "$ZMK_REF" "origin/$ZMK_REF" 2>&1 || git -C "$ZMK_INSTALL_DIR" checkout -B "$ZMK_REF" "$ZMK_REF" 2>&1 || git -C "$ZMK_INSTALL_DIR" checkout "$ZMK_REF" 2>&1

else
  [[ ! -e "$ZMK_INSTALL_DIR" ]] || fail "Target exists but is not a git checkout: $ZMK_INSTALL_DIR"
  info "Downloading a fresh copy of ${ZMK_REF} from GitHub..."
  git clone --depth=1 --branch "$ZMK_REF" --single-branch "$ZMK_GIT_URL" "$ZMK_INSTALL_DIR"
fi

cd "$ZMK_INSTALL_DIR"
chmod +x start.sh install.sh installers/*.sh 2>/dev/null || true

# Remember the branch the launcher should keep tracking.
printf '%s\n' "$ZMK_REF" > "$ZMK_INSTALL_DIR/.zmk-ref"

# Install a short repeatable command. It updates the same Git ref and starts
# the stack, so the operator no longer has to remember a long curl command.
# ZMK_REF=auto lets .zmk-ref drive updates and fall back to main after merge.
ZMK_BIN_DIR="${HOME}/.local/bin"
mkdir -p "$ZMK_BIN_DIR"
printf '#!/usr/bin/env bash\nexec env ZMK_REF=auto ZMK_INSTALL_DIR=%q bash %q "$@"\n' "$ZMK_INSTALL_DIR" "$ZMK_INSTALL_DIR/installers/bootstrap-linux.sh" > "$ZMK_BIN_DIR/zmk-vision"
chmod +x "$ZMK_BIN_DIR/zmk-vision"

# A clean install uses the same host ports as older ZMK stacks. Stop only
# known previous project names (without -v, so data/volumes are preserved)
# unless the operator explicitly wants to keep a parallel stack.
if [[ "${ZMK_KEEP_OLD_STACK:-0}" != "1" ]] && command -v docker >/dev/null 2>&1; then
  for old_project in zmk-videoanalytics zmkvisionfresh zmk-vision; do
    docker compose -p "$old_project" down --remove-orphans >/dev/null 2>&1 || true
  done
fi

# First install asks only once for RTSP credentials (hidden) and then runs the
# regular installer non-interactively: no messenger, no training, inference on.
needs_setup=false
if [[ ! -f .zmk-profiles || ! -f .env ]] || ! grep -qE '^RTSP_CAM_01=rtsps?://' .env 2>/dev/null; then
  needs_setup=true
fi

info "Repeat command installed: ${ZMK_BIN_DIR}/zmk-vision"

if [[ "$needs_setup" == true ]]; then
  echo
  echo "First camera setup. The RTSP URL input is hidden and is stored only in .env."
  # This launcher is normally invoked as `curl ... | bash`, so stdin contains
  # the script itself rather than the user's keyboard. Read credentials from
  # the controlling terminal explicitly; otherwise read returns EOF instantly.
  ZMK_RTSP_URL="${ZMK_RTSP_URL:-}"
  if [[ -z "$ZMK_RTSP_URL" ]]; then
    [[ -r /dev/tty ]] || fail "No interactive terminal detected. Set ZMK_RTSP_URL securely and rerun."
    printf 'RTSP URL (rtsp://...): ' > /dev/tty
    IFS= read -r -s ZMK_RTSP_URL < /dev/tty
    printf '\n' > /dev/tty
  fi
  [[ "$ZMK_RTSP_URL" =~ ^rtsps?://[^[:space:]]+$ ]] || fail "A non-empty rtsp:// or rtsps:// URL is required"
  info "Installing Docker if necessary, configuring the camera and starting ZMK Vision..."
  # bootstrap already fetched the requested Git ref. Do not let the release
  # updater immediately replace a feature branch with the latest main release.
  exec env \
    ZMK_NO_AUTO_UPDATE=1 \
    NONINTERACTIVE=1 \
    MESSENGER_PROVIDER=none \
    ENABLE_INFERENCE=true \
    INFERENCE_DEVICE=cpu \
    RTSP_TRANSPORT=tcp \
    RTSP_TIMEOUT_OPTION=timeout \
    RTSP_CAM_01="$ZMK_RTSP_URL" \
    bash installers/install-linux.sh
fi

info "Configuration already exists. Updating source and starting ZMK Vision..."
# The Git ref above is authoritative for this launcher run (including custom
# arena/feature refs), so skip the release-only updater in start.sh.
exec env ZMK_NO_AUTO_UPDATE=1 bash start.sh
