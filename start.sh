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

# --- auto-update (optional) ---
if [[ "${1:-}" != "--no-update" && -f installers/auto-update.sh ]]; then
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
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then sudo systemctl start docker 2>/dev/null || true; fi
  if ! docker info >/dev/null 2>&1; then DC=(sudo docker compose); fi
fi
"${DC[@]}" version >/dev/null 2>&1 || fail "Docker Compose plugin is unavailable."

if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia; then
  export COMPOSE_FILE="docker-compose.yml:docker-compose.gpu.yml"
  echo "NVIDIA Container Runtime найден: GPU включён"
else
  echo "NVIDIA runtime не найден: workers используют CPU fallback"
fi

wait_http(){ local url="$1" s="${2:-120}" i; for ((i=0;i<s/2;i++)); do curl -fsS --max-time 3 "$url" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }

echo "[start] Запускаю сервисы ZMK Vision..."
"${DC[@]}" --profile telegram --profile max stop telegram-bot max-bot >/dev/null 2>&1 || true
"${DC[@]}" "${PROFILE[@]}" config --quiet || fail "docker-compose.yml или .env не прошли валидацию"
"${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans || "${DC[@]}" "${PROFILE[@]}" logs --tail=100
if ! wait_http http://localhost:8000/api/health 120; then "${DC[@]}" logs --tail=100 api; fail "API health check failed"; fi
if ! wait_http http://localhost:5173 120; then "${DC[@]}" logs --tail=100 web; fail "Web health check failed"; fi

echo ""
echo "ZMK Vision запущено."
echo "Панель:  http://localhost:5173"
echo "API:     http://localhost:8000/docs"
echo "Повторный запуск: ./start.sh"
