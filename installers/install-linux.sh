#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
echo -e "\n=== ZMK Vision one-click installer for Ubuntu/Debian ==="
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker Engine and Compose plugin..."
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl docker.io docker-compose-v2 || sudo apt-get install -y docker.io docker-compose-plugin
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER" || true
fi
DC="docker compose"
if ! docker info >/dev/null 2>&1; then DC="sudo docker compose"; fi
[[ -f .env ]] || cp .env.example .env
read -r -p "Telegram bot token (Enter to skip bot): " TOKEN
if [[ -n "$TOKEN" ]]; then
  read -r -p "Your Telegram numeric ID (admin): " ADMIN
  read -r -p "Public HTTPS Mini App URL (Enter for localhost): " WEBAPP
  printf '\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ADMIN_IDS=%s\n' "$TOKEN" "$ADMIN" >> .env
  [[ -z "$WEBAPP" ]] || printf 'TELEGRAM_WEBAPP_URL=%s\n' "$WEBAPP" >> .env
  $DC --profile telegram up -d --build
else
  $DC up -d --build
fi
echo "ZMK Vision installed successfully."
echo "Dashboard: http://localhost:5173"
echo "API docs:  http://localhost:8000/docs"
