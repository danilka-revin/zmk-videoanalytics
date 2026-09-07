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
#     ./start.sh --fast         — быстрый запуск без принудительной пересборки
#                                 образов (использует уже собранные образы)
#     ./start.sh --rebuild      — принудительно пересобрать образы
#     ./start.sh --no-open      — не открывать браузер (Wayland/X11)
#     NONINTERACTIVE=1 ./start.sh   — без вопросов (нужны env-переменные)
#     ZMK_FAST=1 ./start.sh     — то же, что --fast, через переменную окружения
#     ZMK_REBUILD=1 ./start.sh  — то же, что --rebuild
#     ZMK_NO_OPEN=1 ./start.sh  — то же, что --no-open
# =====================================================================
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

fail(){ echo "ERROR: $*" >&2; exit 1; }

# Flags may be passed in any order: scan the whole argument list instead of
# looking only at $1/$2 (`./start.sh --no-open --rebuild` used to ignore
# --rebuild silently). Branch selection stays positional (see below).
zmk_has_flag(){
  local needle="$1"; shift
  local arg
  for arg in "$@"; do [[ "$arg" == "$needle" ]] && return 0; done
  return 1
}

# Every remote git operation is capped: a stalled fetch used to hang the whole
# launcher with no output, which looks exactly like an endless download.
zmk_git(){
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=15s "${ZMK_GIT_TIMEOUT:-120}" git "$@"
  else
    git "$@"
  fi
}

# Select the Git branch before Docker starts. This is intentionally handled by
# the launcher (not inside a container), so it also works while the old stack
# is running: the selected branch is checked out and the stack is recreated.
select_install_branch(){
  local requested="${ZMK_INSTALL_BRANCH:-}" choice branch choose=0
  if [[ "${1:-}" == "--branch" ]]; then
    [[ -n "${2:-}" ]] || fail "Использование: ./start.sh --branch <ветка>"
    requested="$2"
    shift 2
  elif [[ "${1:-}" == "--choose-branch" ]]; then
    requested=""
    choose=1
    shift
  fi
  [[ -d .git ]] || { [[ -z "$requested" ]] || fail "Для выбора ветки нужен Git-клон проекта"; return 0; }
  if [[ -z "$requested" && ( "$choose" == "1" || "${ZMK_CHOOSE_BRANCH:-0}" == "1" ) ]]; then
    echo "Доступные ветки проекта:"
    mapfile -t branches < <(zmk_git ls-remote --heads origin 2>/dev/null | sed -E 's#.*refs/heads/##' | sort -V)
    ((${#branches[@]})) || fail "Не удалось получить список веток origin"
    local i=1; for branch in "${branches[@]}"; do echo "  $i) $branch"; ((i++)); done
    read -r -p "Выберите номер или введите имя ветки [main]: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice>=1 && choice<=${#branches[@]})); then requested="${branches[$((choice-1))]}"
    elif [[ -n "$choice" ]]; then requested="$choice"; else requested="main"; fi
  fi
  [[ -z "$requested" ]] && return 0
  zmk_git fetch --quiet origin "$requested" || fail "Ветка '$requested' не найдена в origin"
  if ! git diff --quiet || ! git diff --cached --quiet; then fail "Есть незакоммиченные изменения. Сохраните их перед сменой ветки."; fi
  git checkout -q -B "$requested" "origin/$requested" || fail "Не удалось переключиться на ветку '$requested'"
  echo "[start] Установлена ветка: $requested"
}

# --branch/--choose-branch are consumed here before normal startup logic.
select_install_branch "${1:-}" "${2:-}"
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
# Upgrade the selected branch itself. Release archives are used only on main.
if ! zmk_has_flag --no-update "$@" && [[ -z "${ZMK_NO_AUTO_UPDATE:-}" && -f installers/auto-update.sh ]]; then
  if [[ -d .git ]]; then git config --global --add safe.directory "$(pwd)" >/dev/null 2>&1 || true; fi
  ZMK_UPDATE_BRANCH="${ZMK_UPDATE_BRANCH:-$(git branch --show-current 2>/dev/null || true)}" \
    bash installers/auto-update.sh start.sh || echo "[start] auto-update check skipped."
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
if zmk_has_flag --setup "$@"; then
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

# Shared stack helpers: build fingerprinting (no rebuild when nothing changed),
# parallel base-image pre-pull, hard timeouts and visible progress. Without
# them every single start re-ran `npm ci`/`apt-get`/`pip` in every image, which
# is what made the launcher look stuck on an endless download.
if [[ -f installers/lib-stack.sh ]]; then
  # shellcheck disable=SC1091
  source installers/lib-stack.sh
fi

# Desktop helpers (Wayland/X11 aware browser launch).
if [[ -f installers/lib-desktop.sh ]]; then
  # shellcheck disable=SC1091
  source installers/lib-desktop.sh
fi

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
    zmk_git fetch --prune --tags --force origin 2>&1 | tail -3 || true
  fi
}

start_stack(){
  # Timeouts + plain progress: a stalled layer pull must fail with a message
  # instead of showing a frozen installer for an hour.
  stack_prepare_build_env
  # Rebuild ONLY when the sources changed or an image is missing. Previously
  # every start ran `up -d --build`, re-running npm ci / apt-get / pip in every
  # image: on a slow or flaky link that is an endless download.
  if command -v stack_needs_build >/dev/null 2>&1 && ! stack_needs_build; then
    echo "[start] Образы уже собраны для этой версии исходников — пропускаю пересборку (принудительно: ./start.sh --rebuild)"
    if stack_watchdog "$ZMK_BUILD_TIMEOUT" "${DC[@]}" "${PROFILE[@]}" up -d --remove-orphans; then
      stack_save_fingerprint; return 0
    fi
  fi
  # Base images are fetched once, in parallel, instead of one-by-one per service.
  if command -v stack_prepull >/dev/null 2>&1; then stack_prepull; fi
  if stack_watchdog "$ZMK_BUILD_TIMEOUT" "${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans; then
    stack_save_fingerprint; return 0
  fi
  # Known BuildKit failure: a corrupted parent snapshot. Clear ONLY the
  # disposable build cache (never volumes, data or bot tokens) and retry once
  # with serial builds so two services cannot race on the same snapshot.
  repair_build_cache
  echo "[start] Повторная сборка с лимитом параллелизма..."
  if ( export COMPOSE_PARALLEL_LIMIT=1; stack_watchdog "$ZMK_BUILD_TIMEOUT" "${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans ); then
    stack_save_fingerprint; return 0
  fi
  echo "[start] BuildKit всё ещё падает — собираю классическим builder без кэша BuildKit..."
  if ( export DOCKER_BUILDKIT=0 COMPOSE_BAKE=false COMPOSE_PARALLEL_LIMIT=1
       stack_watchdog "$ZMK_BUILD_TIMEOUT" "${DC[@]}" "${PROFILE[@]}" build ); then
    if "${DC[@]}" "${PROFILE[@]}" up -d --remove-orphans; then
      stack_save_fingerprint; return 0
    fi
  fi
  # ultralytics base image unavailable/corrupted: fall back to the slim
  # Dockerfiles (python:3.12-slim + pip) instead of downloading it again.
  if [[ -f services/inference_worker/Dockerfile.slim && -f services/training_worker/Dockerfile.slim ]]; then
    echo "[start] Пробую slim Dockerfiles (python:3.12-slim + pip)..."
    if DOCKER_BUILDKIT=0 "${DOCKER[@]}" build -f services/inference_worker/Dockerfile.slim -t zmk-vision-inference-worker:latest services/inference_worker 2>&1 | tail -20; then
      DOCKER_BUILDKIT=0 "${DOCKER[@]}" build -f services/training_worker/Dockerfile.slim -t zmk-vision-training-worker:latest services/training_worker 2>&1 | tail -10 || true
      if "${DC[@]}" "${PROFILE[@]}" up -d --remove-orphans --no-build; then
        stack_save_fingerprint; return 0
      fi
    fi
  fi
  return 1
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

echo "[start] Запускаю сервисы ZMK Vision..."
"${DC[@]}" "${PROFILE[@]}" config --quiet || fail "docker-compose.yml или .env не прошли валидацию"

# --rebuild / ZMK_REBUILD=1 — принудительная пересборка образов.
# Обычный старт больше не пересобирает стек: installers/lib-stack.sh хранит
# fingerprint исходников, поэтому `npm ci` / `apt-get` / `pip` выполняются
# только когда код действительно изменился. Раньше каждый запуск заново
# скачивал зависимости и установщик «зависал на бесконечной загрузке»;
# теперь повторный запуск занимает секунды, а скачивание ограничено
# ZMK_BUILD_TIMEOUT (по умолчанию 40 минут) и всегда видно в прогрессe.
if zmk_has_flag --rebuild "$@" || [[ "${ZMK_REBUILD:-0}" == "1" ]]; then
  export ZMK_REBUILD=1
  echo "[start] Принудительная пересборка образов (--rebuild)."
fi

if zmk_has_flag --fast "$@" || [[ "${ZMK_FAST:-0}" == "1" ]]; then
  echo "[start] Быстрый запуск: использую уже собранные образы (без --build)."
  if command -v stack_watchdog >/dev/null 2>&1; then
    stack_watchdog "${ZMK_BUILD_TIMEOUT:-600}" "${DC[@]}" "${PROFILE[@]}" up -d --remove-orphans || { "${DC[@]}" "${PROFILE[@]}" logs --tail=100; fail "Docker Compose fast startup failed"; }
  else
    "${DC[@]}" "${PROFILE[@]}" up -d --remove-orphans || { "${DC[@]}" "${PROFILE[@]}" logs --tail=100; fail "Docker Compose fast startup failed"; }
  fi
else
  echo "[start] Собираю/обновляю образы и запускаю сервисы..."
  start_stack || {
    echo ""
    echo "[start] Не удалось собрать образы. Частые причины:"
    echo "  • медленный/недоступный реестр npm — укажите зеркало в .env: NPM_REGISTRY=https://registry.npmmirror.com"
    echo "  • медленный Docker Hub — повторите запуск, предзагрузка базовых образов идёт параллельно"
    echo "  • нет сети — запустите офлайн: ./start.sh --fast"
    "${DC[@]}" "${PROFILE[@]}" logs --tail=60
    fail "Docker Compose startup failed after BuildKit cache recovery"
  }
fi
if ! wait_http http://localhost:8000/api/health 120; then "${DC[@]}" logs --tail=100 api; fail "API health check failed"; fi
if ! wait_http http://localhost:5173 120; then "${DC[@]}" logs --tail=100 web; fail "Web health check failed"; fi

# Open the panel in the desktop session. On Wayland this has to run with the
# logged-in user's WAYLAND_DISPLAY/XDG_RUNTIME_DIR/DBUS bus, otherwise
# xdg-open (called through sudo for Docker) silently does nothing.
if ! zmk_has_flag --no-open "$@"; then
  # Guarded: an older release archive may not ship lib-desktop.sh yet.
  if command -v zmk_wayland_hint >/dev/null 2>&1; then zmk_wayland_hint; fi
  if command -v zmk_open_url >/dev/null 2>&1; then zmk_open_url "http://localhost:5173"; fi
fi

print_launch_summary
