#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail(){ echo "ERROR: $*" >&2; exit 1; }
required=(docker-compose.yml .env.example backend/Dockerfile frontend/Dockerfile services/telegram_bot/Dockerfile services/max_bot/Dockerfile services/training_worker/Dockerfile services/inference_worker/Dockerfile)
for file in "${required[@]}"; do [[ -f "$file" ]] || fail "Missing $file. Download and extract the complete release archive, not only the installer."; done

if [[ "${1:-}" == "--check" ]]; then
  echo "Project files: OK"
  bash -n installers/install-linux.sh installers/uninstall-linux.sh
  if command -v docker >/dev/null 2>&1; then docker compose version && docker compose config --quiet || fail "Docker Compose validation failed"; else echo "WARNING: Docker is not installed; project file validation only."; fi
  echo "Installer validation: OK"
  exit 0
fi

echo -e "\n=== ZMK Vision installer for Ubuntu/Debian ==="
if [[ "$(uname -s)" != "Linux" ]]; then fail "This installer supports Linux only"; fi
if ! command -v apt-get >/dev/null 2>&1; then fail "Automatic installation supports Ubuntu/Debian (apt). Install Docker manually on this distribution."; fi
if ! command -v curl >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y ca-certificates curl; fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker Engine and Compose plugin..."
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl docker.io
  sudo apt-get install -y docker-compose-v2 || sudo apt-get install -y docker-compose-plugin
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER" || true
fi
command -v docker >/dev/null 2>&1 || fail "Docker CLI is unavailable"
DC=(docker compose)
if ! docker info >/dev/null 2>&1; then
  sudo systemctl start docker || true
  if ! docker info >/dev/null 2>&1; then DC=(sudo docker compose); fi
fi
"${DC[@]}" version >/dev/null || fail "Docker Compose plugin is unavailable"
[[ -f .env ]] || cp .env.example .env
chmod 600 .env

set_env(){
  local key="$1" value="$2" escaped
  escaped="${value//|/\\|}"
  if grep -q "^${key}=" .env; then sed -i "s|^${key}=.*|${key}=${escaped}|" .env; else printf '%s=%s\n' "$key" "$value" >> .env; fi
}
wait_http(){
  local url="$1" seconds="${2:-120}" i
  for ((i=0;i<seconds/2;i++)); do curl -fsS --max-time 3 "$url" >/dev/null 2>&1 && return 0; sleep 2; done
  return 1
}

if [[ -n "${NONINTERACTIVE:-}" ]]; then
  MESSENGER="${MESSENGER_PROVIDER:-none}"
else
  echo "Выберите мессенджер: 1 — Telegram, 2 — MAX, 0 — без бота"
  read -r -p "Ваш выбор [0/1/2]: " CHOICE
  case "$CHOICE" in 1) MESSENGER=telegram;; 2) MESSENGER=max;; 0|"") MESSENGER=none;; *) fail "Неизвестный вариант мессенджера";; esac
fi
PROFILE=()
case "$MESSENGER" in
  telegram)
    if [[ -n "${NONINTERACTIVE:-}" ]]; then TOKEN="${TELEGRAM_BOT_TOKEN:-}"; ADMIN="${TELEGRAM_ADMIN_IDS:-}"; WEBAPP="${TELEGRAM_WEBAPP_URL:-}"; else read -r -p "Telegram bot token: " TOKEN; read -r -p "Ваш Telegram numeric ID (admin): " ADMIN; read -r -p "Public HTTPS Mini App URL (Enter для bot-only): " WEBAPP; fi
    [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || fail "Telegram token format is invalid"
    [[ "$ADMIN" =~ ^[0-9]+(,[0-9]+)*$ ]] || fail "Telegram admin ID must contain numeric IDs separated by commas"
    [[ -z "$WEBAPP" || "$WEBAPP" == https://* ]] || fail "Telegram Mini App URL must use HTTPS"
    set_env TELEGRAM_BOT_TOKEN "$TOKEN"; set_env TELEGRAM_ADMIN_IDS "$ADMIN"; set_env TELEGRAM_WEBAPP_URL "$WEBAPP"
    set_env MAX_BOT_TOKEN ""; set_env MESSENGER_PROVIDER "telegram"
    PROFILE=(--profile telegram)
    ;;
  max)
    if [[ -n "${NONINTERACTIVE:-}" ]]; then TOKEN="${MAX_BOT_TOKEN:-}"; ADMIN="${MAX_ADMIN_IDS:-}"; else read -r -p "MAX bot token от @MasterBot: " TOKEN; read -r -p "Ваш MAX numeric ID (admin): " ADMIN; fi
    [[ "$TOKEN" =~ ^[A-Za-z0-9._:-]{20,500}$ ]] || fail "MAX token format is invalid"
    [[ "$ADMIN" =~ ^[0-9]+(,[0-9]+)*$ ]] || fail "MAX admin ID must contain numeric IDs separated by commas"
    set_env MAX_BOT_TOKEN "$TOKEN"; set_env MAX_ADMIN_IDS "$ADMIN"
    set_env TELEGRAM_BOT_TOKEN ""; set_env MESSENGER_PROVIDER "max"
    PROFILE=(--profile max)
    ;;
  none) set_env TELEGRAM_BOT_TOKEN ""; set_env MAX_BOT_TOKEN ""; set_env MESSENGER_PROVIDER "none";;
  *) fail "MESSENGER_PROVIDER must be telegram, max or none";;
esac
"${DC[@]}" --profile telegram --profile max stop telegram-bot max-bot >/dev/null 2>&1 || true
if [[ -n "${NONINTERACTIVE:-}" ]]; then TRAINING="${ENABLE_TRAINING:-false}"; else read -r -p "Включить реальное YOLO auto-training на NVIDIA GPU? [y/N]: " TRAINING; fi
if [[ "$TRAINING" =~ ^([yY]|true|1)$ ]]; then
  command -v nvidia-smi >/dev/null 2>&1 || echo "WARNING: nvidia-smi не найден; установите NVIDIA driver и Container Toolkit"
  set_env TRAINING_WORKER_URL "http://training-worker:8010"
  PROFILE+=(--profile training)
else
  set_env TRAINING_WORKER_URL ""
fi
if [[ -n "${NONINTERACTIVE:-}" ]]; then INFERENCE="${ENABLE_INFERENCE:-false}"; else read -r -p "Включить реальный RTSP YOLO inference worker? [y/N]: " INFERENCE; fi
if [[ "$INFERENCE" =~ ^([yY]|true|1)$ ]]; then
  TOKEN_VALUE="${ZMK_WORKER_TOKEN:-$(tr -d '-' </proc/sys/kernel/random/uuid)}"
  set_env ZMK_WORKER_TOKEN "$TOKEN_VALUE"
  PROFILE+=(--profile inference)
fi

"${DC[@]}" "${PROFILE[@]}" config --quiet || fail "docker-compose.yml or .env validation failed"
if ! "${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans; then "${DC[@]}" "${PROFILE[@]}" logs --tail=100; fail "Docker Compose startup failed"; fi
if ! wait_http http://localhost:8000/api/health 120; then "${DC[@]}" logs --tail=100 api; fail "API health check failed"; fi
if ! wait_http http://localhost:5173 120; then "${DC[@]}" logs --tail=100 web; fail "Web health check failed"; fi

echo "ZMK Vision installed and verified successfully."
echo "Dashboard: http://localhost:5173"
echo "API docs:  http://localhost:8000/docs"
