#!/usr/bin/env bash
# =====================================================================
# ZMK Vision launcher (Linux).
#
# On every start this launcher:
#   1. Checks for a newer GitHub release.
#   2. If one exists: downloads it, verifies the SHA256 checksum,
#      unpacks it and re-launches this same script with the new build.
#   3. Otherwise (or after an update) it starts the stack with Docker.
#
# Set ZMK_NO_AUTO_UPDATE=1 to skip the version check (e.g. fully offline).
# =====================================================================
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

fail(){ echo "ERROR: $*" >&2; exit 1; }

if [[ -f installers/auto-update.sh ]]; then
  # This may (a) apply an update and exec the new code, or (b) return.
  bash installers/auto-update.sh start.sh || echo "[start] auto-update check skipped."
fi

required=(docker-compose.yml .env.example backend/Dockerfile frontend/Dockerfile)
for f in "${required[@]}"; do [[ -f "$f" ]] || fail "Missing $f. Run install-linux.sh first."; done

DC=(docker compose)
if ! command -v docker >/dev/null 2>&1; then fail "Docker CLI is not installed."; fi
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then DC=(sudo docker compose); else fail "Docker is not running and sudo is unavailable."; fi
fi
"${DC[@]}" version >/dev/null 2>&1 || fail "Docker Compose plugin is unavailable."

PROFILE=()
if [[ -f .zmk-profiles ]]; then
  mapfile -t PROFILE < .zmk-profiles
fi

if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia; then
  export COMPOSE_FILE="docker-compose.yml:docker-compose.gpu.yml"
  echo "NVIDIA Container Runtime found: GPU enabled"
else
  echo "NVIDIA runtime not found: workers use CPU fallback"
fi

wait_http(){ local url="$1" s="${2:-120}" i; for ((i=0;i<s/2;i++)); do curl -fsS --max-time 3 "$url" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }

echo "[start] Starting ZMK Vision services..."
"${DC[@]}" "${PROFILE[@]}" config --quiet || fail "docker-compose.yml or .env validation failed"
"${DC[@]}" "${PROFILE[@]}" up -d --build --remove-orphans || "${DC[@]}" "${PROFILE[@]}" logs --tail=100
if ! wait_http http://localhost:8000/api/health 120; then "${DC[@]}" logs --tail=100 api; fail "API health check failed"; fi
if ! wait_http http://localhost:5173 120; then "${DC[@]}" logs --tail=100 web; fail "Web health check failed"; fi

echo "ZMK Vision is running."
echo "Dashboard: http://localhost:5173"
echo "API docs:  http://localhost:8000/docs"
