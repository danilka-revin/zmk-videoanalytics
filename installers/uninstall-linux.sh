#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null 2>&1 || { echo "Docker is not installed" >&2; exit 1; }
DC=(docker compose)
docker info >/dev/null 2>&1 || DC=(sudo docker compose)
"${DC[@]}" --profile telegram --profile production down --remove-orphans
if [[ "${1:-}" == "--purge" ]]; then
  read -r -p "Delete persistent database and volumes? Type DELETE: " answer
  if [[ "$answer" == "DELETE" ]]; then
    "${DC[@]}" --profile telegram --profile production down -v --remove-orphans
    rm -rf data
  fi
fi
echo "ZMK Vision services stopped. Data preserved unless --purge was confirmed."
