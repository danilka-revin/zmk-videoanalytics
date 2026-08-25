#!/usr/bin/env bash
# =====================================================================
# ZMK Vision installer (Linux). Uses the shared config wizard.
#
#   bash installers/install-linux.sh            # install deps + config + run
#   bash installers/install-linux.sh --setup    # config ONLY (no run)
#   bash installers/install-linux.sh --check    # validate only
#   NONINTERACTIVE=1 ...                        # unattended
#
# The same wizard is used by start.sh, so the whole project starts with ONE
# command:  ./start.sh
# =====================================================================
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail(){ echo "ERROR: $*" >&2; exit 1; }
run_privileged(){
  if [[ "${EUID}" -eq 0 ]]; then "$@"; else command -v sudo >/dev/null 2>&1 || fail "sudo is required to install Docker"; sudo "$@"; fi
}
required=(docker-compose.yml .env.example backend/Dockerfile frontend/Dockerfile services/telegram_bot/Dockerfile services/max_bot/Dockerfile services/training_worker/Dockerfile services/inference_worker/Dockerfile)
for file in "${required[@]}"; do [[ -f "$file" ]] || fail "Missing $file. Download and extract the complete release archive, not only the installer."; done

if [[ "${1:-}" == "--check" ]]; then
  echo "Project files: OK"
  bash -n installers/install-linux.sh installers/uninstall-linux.sh installers/auto-update.sh installers/wizard.sh start.sh
  if command -v docker >/dev/null 2>&1; then docker compose version && docker compose config --quiet || fail "Docker Compose validation failed"; else echo "WARNING: Docker is not installed; project file validation only."; fi
  echo "Installer validation: OK"
  exit 0
fi

wait_http(){
  local url="$1" seconds="${2:-120}" i
  for ((i=0;i<seconds/2;i++)); do curl -fsS --max-time 3 "$url" >/dev/null 2>&1 && return 0; sleep 2; done
  return 1
}

# --- auto-update (install flow) ---
if [[ -n "${ZMK_NO_AUTO_UPDATE:-}" && "${ZMK_NO_AUTO_UPDATE}" == "1" ]]; then
  echo "Auto-update disabled via ZMK_NO_AUTO_UPDATE=1."
elif [[ "${ZMK_RELAUNCHED_AFTER_UPDATE:-}" == "1" ]]; then
  :
elif [[ -f installers/auto-update.sh ]]; then
  bash installers/auto-update.sh installers/install-linux.sh || echo "[install] auto-update check skipped."
fi

# shellcheck disable=SC1091
source installers/wizard.sh

case "${1:-}" in
  --setup)
    echo "=== ZMK Vision: настройка конфигурации ==="
    run_config || fail "Настройка не завершена"
    exit 0
    ;;
esac

echo -e "\n=== ZMK Vision installer for Ubuntu/Debian ==="
if [[ "$(uname -s)" != "Linux" ]]; then fail "This installer supports Linux only"; fi
if ! command -v apt-get >/dev/null 2>&1; then fail "Automatic installation supports Ubuntu/Debian (apt). Install Docker manually on this distribution."; fi
if ! command -v curl >/dev/null 2>&1; then run_privileged apt-get update && run_privileged apt-get install -y ca-certificates curl; fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker Engine and Compose plugin..."
  run_privileged apt-get update
  run_privileged apt-get install -y ca-certificates curl docker.io
  run_privileged apt-get install -y docker-compose-v2 || run_privileged apt-get install -y docker-compose-plugin
  run_privileged systemctl enable --now docker
  if [[ "${EUID}" -ne 0 ]]; then run_privileged usermod -aG docker "$USER" || true; fi
fi

run_config || fail "Настройка не завершена"

[[ -f .env ]] || cp .env.example .env
chmod 600 .env 2>/dev/null || true
command -v docker >/dev/null 2>&1 || fail "Docker CLI is unavailable"
DC=(docker compose)
if ! docker info >/dev/null 2>&1; then
  run_privileged systemctl start docker 2>/dev/null || true
  if ! docker info >/dev/null 2>&1; then
    if [[ "${EUID}" -eq 0 ]]; then DC=(docker compose); else DC=(sudo docker compose); fi
  fi
fi
"${DC[@]}" version >/dev/null || fail "Docker Compose plugin is unavailable"

PROFILE=()
if [[ -f .zmk-profiles ]]; then mapfile -t PROFILE < .zmk-profiles; fi
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia; then
  export COMPOSE_FILE="docker-compose.yml:docker-compose.gpu.yml"
  echo "NVIDIA Container Runtime найден: workers получат GPU"
else
  echo "NVIDIA Container Runtime не найден: workers запустятся в CPU fallback без ошибки"
fi

"${DC[@]}" "${PROFILE[@]}" config --quiet || fail "docker-compose.yml or .env validation failed"
if ! "${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans; then "${DC[@]}" "${PROFILE[@]}" logs --tail=100; fail "Docker Compose startup failed"; fi
if ! wait_http http://localhost:8000/api/health 120; then "${DC[@]}" logs --tail=100 api; fail "API health check failed"; fi
if ! wait_http http://localhost:5173 120; then "${DC[@]}" logs --tail=100 web; fail "Web health check failed"; fi

echo ""
echo "ZMK Vision installed and verified successfully."
echo "Dashboard: http://localhost:5173"
echo "API docs:  http://localhost:8000/docs"
echo "Next starts: ./start.sh  (checks for updates automatically)"
