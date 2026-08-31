#!/usr/bin/env bash
# =====================================================================
# ZMK Vision — ЕДИНАЯ ТОЧКА ЗАПУСКА (Linux). Один клик:  ./start.sh
#
#   1) Проверяет/скачивает новую версию с GitHub (если есть).
#   2) На ПЕРВОМ запуске открывает мастер настройки:
#        • мессенджер (Telegram / MAX / без бота)  ← то, что ты искал
#        • токены бота
#        • training / inference workers
#        • токены безопасности
#      Всё сохраняется в .env и .zmk-profiles.
#   3) Запускает Docker-стек.
#
#   Повторные запуски — без вопросов, сразу старт.
#
#   Полезно:
#     ./start.sh --setup        — переоткрыть мастер настройки
#     ./start.sh --check        — только проверка
#     ./start.sh --no-update    — пропустить проверку обновлений
#     NONINTERACTIVE=1 ./start.sh   — без вопросов (нужны env-переменные)
# =====================================================================
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

fail(){ echo "ERROR: $*" >&2; exit 1; }
run_privileged(){
  if [[ "${EUID}" -eq 0 ]]; then "$@"; else command -v sudo >/dev/null 2>&1 || fail "sudo is required to start Docker"; sudo "$@"; fi
}

# --- auto-update (optional) ---
# Fix dubious ownership + divergent branches fatal error for users who ran `git pull` manually
# This is critical when /root/zmk-vision is owned by different UID or when running via sudo
if [[ -d .git ]]; then
  git config --global --add safe.directory "$(pwd)" >/dev/null 2>&1 || true
  if command -v sudo >/dev/null 2>&1; then sudo git config --global --add safe.directory "$(pwd)" >/dev/null 2>&1 || true; fi
  git config pull.rebase false >/dev/null 2>&1 || true
  git config pull.ff only >/dev/null 2>&1 || true
fi
# Release updater is intentionally skipped on feature branches. Otherwise a
# one-command launch re-checkouts main and silently removes the current
# go2rtc/WebRTC build. main is the only channel auto-update may rewrite.
if [[ "${1:-}" != "--no-update" && -z "${ZMK_NO_AUTO_UPDATE:-}" && -f installers/auto-update.sh ]]; then
  if [[ -d .git ]]; then git config --global --add safe.directory "$(pwd)" >/dev/null 2>&1 || true; fi
  if [[ -d .git ]] && git branch --show-current 2>/dev/null | grep -qvE '^(main|master)$'; then
    echo "[start] Feature branch active; release auto-update is skipped to preserve this build."
  else
    bash installers/auto-update.sh start.sh || echo "[start] auto-update check skipped."
  fi
fi

# required project files
required=(docker-compose.yml .env.example backend/Dockerfile frontend/Dockerfile)
for f in "${required[@]}"; do [[ -f "$f" ]] || fail "Отсутствует файл $f. Распакуйте полный архив или запустите установщик.";
done

[[ -f .env ]] || cp .env.example .env
chmod 600 .env 2>/dev/null || true

# =====================================================================
# FIRST-RUN CONFIGURATION WIZARD  (before the docker check so it always runs)
# =====================================================================
wizard_needed=false
if [[ "${1:-}" == "--setup" ]]; then
  wizard_needed=true
elif [[ ! -f .zmk-profiles ]]; then
  # No saved profile -> this is a first run -> ask how to configure.
  wizard_needed=true
fi

if [[ "$wizard_needed" == "true" ]]; then
  echo ""
  echo "═══ Первый запуск ZMK Vision — настройка ═══"
  # shellcheck disable=SC1091
  source installers/wizard.sh
  run_config || { echo "Настройка не завершена — выходим."; exit 1; }
else
  echo "[start] Конфигурация уже задана — запускаю как есть (повторить мастер: ./start.sh --setup)"
fi

# --- docker available? (after wizard so first-run always configures) ---
command -v docker >/dev/null 2>&1 || fail "Docker не установлен. Выполните:  sudo apt install -y docker.io docker-compose-v2  (или запустите установщик)."

# =====================================================================
# RUN
# =====================================================================
PROFILE=()
if [[ -f .zmk-profiles ]]; then mapfile -t PROFILE < .zmk-profiles; fi

DC=(docker compose)
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  run_privileged systemctl start docker 2>/dev/null || true
  if ! docker info >/dev/null 2>&1; then
    if [[ "${EUID}" -eq 0 ]]; then DC=(docker compose); else DC=(sudo docker compose); DOCKER=(sudo docker); fi
  fi
fi
"${DC[@]}" version >/dev/null 2>&1 || fail "Docker Compose plugin is unavailable."

if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia; then
  export COMPOSE_FILE="docker-compose.yml:docker-compose.gpu.yml"
  COMPUTE_MODE="GPU / NVIDIA"
  echo "NVIDIA Container Runtime найден: GPU включён"
else
  COMPUTE_MODE="CPU FALLBACK"
  echo "NVIDIA runtime не найден: workers используют CPU fallback"
fi

wait_http(){ local url="$1" s="${2:-120}" i; for ((i=0;i<s/2;i++)); do curl -fsS --max-time 3 "$url" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }

repair_build_cache(){
  # BuildKit's occasional "parent snapshot ... does not exist" is cache
  # corruption, not a project/data error. Remove only disposable build cache;
  # never touch named volumes, data, models or bot tokens.
  echo "[start] Docker BuildKit не собрал образы. Очищаю только кэш сборки и повторяю один раз..."
  "${DOCKER[@]}" builder prune -af >/dev/null 2>&1 || true
  "${DOCKER[@]}" builder prune --all -f >/dev/null 2>&1 || true
  "${DOCKER[@]}" buildx prune -af >/dev/null 2>&1 || true
  # Also clear Buildx cache that can hold corrupted parent snapshots
  "${DOCKER[@]}" system prune -f --filter "until=24h" >/dev/null 2>&1 || true
  run_privileged systemctl restart docker 2>/dev/null || true
  sleep 2
  # Fix divergent git state that can break future pulls
  if [[ -d .git ]]; then
    git config --global --add safe.directory "$(pwd)" >/dev/null 2>&1 || true
    if command -v sudo >/dev/null 2>&1; then sudo git config --global --add safe.directory "$(pwd)" >/dev/null 2>&1 || true; fi
    git config pull.rebase false >/dev/null 2>&1 || true
    git config pull.ff only >/dev/null 2>&1 || true
    # Don't auto-reset here, just fetch to fix divergent state
    git fetch --prune --tags --force origin 2>&1 | tail -3 || true
  fi
}

start_stack(){
  # Always disable Bake to avoid WARN when buildx isn't installed - use classic builder path
  export COMPOSE_BAKE=false
  export COMPOSE_DOCKER_CLI_BUILD=0
  # First attempt with Bake disabled to avoid warning and use reliable path
  if COMPOSE_BAKE=false COMPOSE_DOCKER_CLI_BUILD=0 "${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans; then
    return 0
  fi
  # Fallback to default (with Bake) in case user has buildx
  if "${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans; then
    return 0
  fi
  repair_build_cache
  # Serial service builds avoid a second concurrent snapshot/export race.
  echo "[start] Повторная сборка с лимитом параллелизма..."
  if COMPOSE_PARALLEL_LIMIT=1 COMPOSE_BAKE=false COMPOSE_DOCKER_CLI_BUILD=0 "${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans; then
    return 0
  fi
  echo "[start] BuildKit всё ещё падает, пробую без BuildKit и без Bake..."
  # Fallback 1: disable Bake (the warning about buildx not installed)
  if COMPOSE_BAKE=false COMPOSE_DOCKER_CLI_BUILD=0 "${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans; then
    return 0
  fi
  echo "[start] Пробую сборку без кэша для inference-worker..."
  # Fallback 2: no-cache for the failing worker images
  if DOCKER_BUILDKIT=0 COMPOSE_BAKE=false "${DC[@]}" "${PROFILE[@]}" build --no-cache inference-worker training-worker 2>&1 | tail -20; then
    if DOCKER_BUILDKIT=0 COMPOSE_BAKE=false "${DC[@]}" "${PROFILE[@]}" up -d --remove-orphans; then
      return 0
    fi
  fi
  echo "[start] Последняя попытка: полная очистка builder и no-cache..."
  "${DOCKER[@]}" builder prune --all -f >/dev/null 2>&1 || true
  if DOCKER_BUILDKIT=0 COMPOSE_BAKE=false COMPOSE_PARALLEL_LIMIT=1 "${DC[@]}" "${PROFILE[@]}" up -d --build --no-cache --remove-orphans; then
    return 0
  fi
  echo "[start] Пробую slim Dockerfiles (python:3.12-slim + pip) как fallback для ultralytics base..."
  # Build workers with slim Dockerfile if ultralytics base keeps failing (parent snapshot corruption or slow pip)
  if [[ -f services/inference_worker/Dockerfile.slim && -f services/training_worker/Dockerfile.slim ]]; then
    echo "[start] Собираю inference-worker с Dockerfile.slim..."
    if DOCKER_BUILDKIT=0 "${DOCKER[@]}" build -f services/inference_worker/Dockerfile.slim -t zmk-vision-inference-worker:latest services/inference_worker 2>&1 | tail -30; then
      echo "[start] inference-worker slim собран, собираю training-worker..."
      DOCKER_BUILDKIT=0 "${DOCKER[@]}" build -f services/training_worker/Dockerfile.slim -t zmk-vision-training-worker:latest services/training_worker 2>&1 | tail -20 || true
      # Now up with --no-build to use the manually built images
      if "${DC[@]}" "${PROFILE[@]}" up -d --remove-orphans --no-build; then
        return 0
      fi
      # Fallback: try compose up with build but using slim via override
      echo "[start] Пробую compose с slim через временный Dockerfile..."
      cp services/inference_worker/Dockerfile services/inference_worker/Dockerfile.bak 2>/dev/null || true
      cp services/training_worker/Dockerfile services/training_worker/Dockerfile.bak 2>/dev/null || true
      cp services/inference_worker/Dockerfile.slim services/inference_worker/Dockerfile
      cp services/training_worker/Dockerfile.slim services/training_worker/Dockerfile
      if DOCKER_BUILDKIT=0 COMPOSE_BAKE=false "${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans; then
        # Restore original Dockerfiles
        mv services/inference_worker/Dockerfile.bak services/inference_worker/Dockerfile 2>/dev/null || true
        mv services/training_worker/Dockerfile.bak services/training_worker/Dockerfile 2>/dev/null || true
        return 0
      fi
      mv services/inference_worker/Dockerfile.bak services/inference_worker/Dockerfile 2>/dev/null || true
      mv services/training_worker/Dockerfile.bak services/training_worker/Dockerfile 2>/dev/null || true
    fi
  fi
  # Final attempt: try with parallel limit 1 and no cache, classic builder
  DOCKER_BUILDKIT=0 COMPOSE_BAKE=false COMPOSE_PARALLEL_LIMIT=1 "${DC[@]}" "${PROFILE[@]}" up -d --build --no-cache --remove-orphans
}

print_zmk_logo(){
  printf '%s\n' \
    ' ███████╗███╗   ███╗██╗  ██╗    ██╗   ██╗██╗███████╗██╗ ██████╗ ███╗   ██╗' \
    ' ╚══███╔╝████╗ ████║██║ ██╔╝    ██║   ██║██║██╔════╝██║██╔═══██╗████╗  ██║' \
    '   ███╔╝ ██╔████╔██║█████╔╝     ██║   ██║██║███████╗██║██║   ██║██╔██╗ ██║' \
    '  ███╔╝  ██║╚██╔╝██║██╔═██╗     ╚██╗ ██╔╝██║╚════██║██║██║   ██║██║╚██╗██║' \
    ' ███████╗██║ ╚═╝ ██║██║  ██╗     ╚████╔╝ ██║███████║██║╚██████╔╝██║ ╚████║' \
    ' ╚══════╝╚═╝     ╚═╝╚═╝  ╚═╝      ╚═══╝  ╚═╝╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝'
}

print_launch_summary(){
  local version ref revision profile compose_display launch_command
  version=$(tr -d '[:space:]' < VERSION 2>/dev/null || printf 'DEV')
  if command -v git >/dev/null 2>&1 && [[ -d .git ]]; then
    ref=$(git branch --show-current 2>/dev/null || printf 'DETACHED')
    revision=$(git rev-parse --short HEAD 2>/dev/null || printf 'UNKNOWN')
  else
    ref="RELEASE ARCHIVE"; revision="N/A"
  fi
  profile="${PROFILE[*]:-DEFAULT SERVICES}"
  compose_display=$(printf '%q ' "${DC[@]}" "${PROFILE[@]}")
  launch_command="${HOME}/.local/bin/zmk-vision"
  [[ -x "$launch_command" ]] || launch_command="./start.sh (launcher is created by bootstrap)"
  printf '\n%s\n' '================================================================'
  print_zmk_logo
  printf '%s\n' '                           ZMK VISION'
  printf '%s\n' '                 VIDEO ANALYTICS CONTROL PLATFORM'
  printf '%s\n' '================================================================'
  printf ' STATUS              : RUNNING\n'
  printf ' VERSION             : %s\n' "$version"
  printf ' SOURCE BRANCH       : %s (%s)\n' "$ref" "$revision"
  printf ' PROJECT DIRECTORY   : %s\n' "$ROOT"
  printf ' COMPUTE MODE        : %s\n' "$COMPUTE_MODE"
  printf ' COMPOSE PROFILES    : %s\n' "$profile"
  printf '%s\n' '----------------------------------------------------------------'
  printf ' WEB PANEL           : http://localhost:5173\n'
  printf ' TELEGRAM MINI APP   : http://localhost:5173/telegram\n'
  printf ' API DOCUMENTATION   : http://localhost:8000/docs\n'
  printf ' API HEALTH CHECK    : http://localhost:8000/api/health\n'
  printf ' WEBRTC UPSTREAM     : GO2RTC_ENABLED=%s GO2RTC_UPSTREAM=%s\n' "${GO2RTC_ENABLED:-true}" "${GO2RTC_UPSTREAM:-http://host.docker.internal:1984}"
  printf '%s\n' '----------------------------------------------------------------'
  printf ' UPDATE / START      : %s\n' "$launch_command"
  printf ' PROJECT START       : ./start.sh\n'
  printf ' LIVE LOGS           : %slogs -f\n' "$compose_display"
  printf ' AI WORKER LOGS      : %slogs -f inference-worker\n' "$compose_display"
  printf ' STOP SERVICES       : %sdown\n' "$compose_display"
  printf '%s\n' '----------------------------------------------------------------'
  printf '%s\n' ' SERVICE STATUS'
  "${DC[@]}" "${PROFILE[@]}" ps 2>/dev/null || true
  printf '%s\n\n' '================================================================'
}

echo "[start] Обновляю образы и запускаю сервисы ZMK Vision..."
"${DC[@]}" "${PROFILE[@]}" config --quiet || fail "docker-compose.yml или .env не прошли валидацию"
start_stack || { "${DC[@]}" "${PROFILE[@]}" logs --tail=100; fail "Docker Compose startup failed after BuildKit cache recovery"; }
if ! wait_http http://localhost:8000/api/health 120; then "${DC[@]}" logs --tail=100 api; fail "API health check failed"; fi
if ! wait_http http://localhost:5173 120; then "${DC[@]}" logs --tail=100 web; fail "Web health check failed"; fi

print_launch_summary
