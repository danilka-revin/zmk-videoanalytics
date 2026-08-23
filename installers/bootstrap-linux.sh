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
ZMK_REF="${ZMK_REF:-main}"
ZMK_INSTALL_DIR="${ZMK_INSTALL_DIR:-$HOME/zmk-vision}"
ZMK_GIT_URL="https://github.com/${ZMK_REPO}.git"

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

if [[ "${1:-}" == "--check" ]]; then
  echo "ZMK bootstrap launcher: OK"
  exit 0
fi

ensure_command git git
ensure_command curl curl

if [[ -d "$ZMK_INSTALL_DIR/.git" ]]; then
  info "Existing installation found: $ZMK_INSTALL_DIR"
  current_origin=$(git -C "$ZMK_INSTALL_DIR" remote get-url origin 2>/dev/null || true)
  [[ "$current_origin" == "$ZMK_GIT_URL" ]] || fail "The target has another git origin: ${current_origin}"
  changes=$(git -C "$ZMK_INSTALL_DIR" status --porcelain --untracked-files=no)
  [[ -z "$changes" ]] || fail "Tracked local changes found. They were not overwritten; commit or stash them first."
  info "Downloading ${ZMK_REF} from GitHub..."
  git -C "$ZMK_INSTALL_DIR" fetch --depth=1 origin "$ZMK_REF"
  git -C "$ZMK_INSTALL_DIR" checkout -B "$ZMK_REF" "origin/$ZMK_REF"
else
  [[ ! -e "$ZMK_INSTALL_DIR" ]] || fail "Target exists but is not a git checkout: $ZMK_INSTALL_DIR"
  info "Downloading a fresh copy of ${ZMK_REF} from GitHub..."
  git clone --depth=1 --branch "$ZMK_REF" --single-branch "$ZMK_GIT_URL" "$ZMK_INSTALL_DIR"
fi

cd "$ZMK_INSTALL_DIR"
chmod +x start.sh install.sh installers/*.sh 2>/dev/null || true

# Install a short repeatable command. It updates the same Git ref and starts
# the stack, so the operator no longer has to remember a long curl command.
ZMK_BIN_DIR="${HOME}/.local/bin"
mkdir -p "$ZMK_BIN_DIR"
printf '#!/usr/bin/env bash\nexec env ZMK_REF=%q ZMK_INSTALL_DIR=%q bash %q "$@"\n' "$ZMK_REF" "$ZMK_INSTALL_DIR" "$ZMK_INSTALL_DIR/installers/bootstrap-linux.sh" > "$ZMK_BIN_DIR/zmk-vision"
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
  exec env \
    NONINTERACTIVE=1 \
    MESSENGER_PROVIDER=none \
    ENABLE_TRAINING=false \
    ENABLE_INFERENCE=true \
    INFERENCE_DEVICE=cpu \
    RTSP_TRANSPORT=tcp \
    RTSP_TIMEOUT_OPTION=timeout \
    RTSP_CAM_01="$ZMK_RTSP_URL" \
    bash installers/install-linux.sh
fi

info "Configuration already exists. Updating source and starting ZMK Vision..."
exec bash start.sh
