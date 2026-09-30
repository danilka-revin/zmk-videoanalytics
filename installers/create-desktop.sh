#!/usr/bin/env bash
# =====================================================================
# Zovod — create an Ubuntu desktop/menu shortcut.
#
# The shortcut runs start.sh, which checks GitHub for the newest commit of the
# tracked branch (any commit or merge is a new version),
# applies a verified update when available, and then starts/recreates the
# Docker Compose services. Runtime data and Docker volumes are preserved.
#
# Usage: bash installers/create-desktop.sh [path-to-project]
# =====================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_PATH="$(cd "${1:-$ROOT}" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
APP_FILE="$APP_DIR/zmk-vision.desktop"
ICON="$PROJECT_PATH/frontend/public/logo.svg"

fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ -f "$PROJECT_PATH/start.sh" ]] || fail "start.sh не найден в $PROJECT_PATH"

# Desktop Entry Exec values use their own quoting rules (not shell quoting).
desktop_quote(){
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//\$/\\\$}
  value=${value//\`/\\\`}
  printf '"%s"' "$value"
}

mkdir -p "$APP_DIR" "$HOME/.local/share/icons"
# Use the same bundled factory emblem as the web app and browser favicon.
if [[ -f "$ICON" ]]; then ICON_ENTRY="$ICON"; else ICON_ENTRY="applications-multimedia"; fi
PROJECT_EXEC=$(desktop_quote "$PROJECT_PATH/start.sh")
ICON_EXEC=$(desktop_quote "$ICON_ENTRY")
write_launcher(){
  local file="$1"
  cat > "$file" <<EOF
[Desktop Entry]
Type=Application
Name=Zovod — обновить и запустить
Comment=Скачать последнюю версию и перезапустить сервисы Docker
Exec=bash $PROJECT_EXEC
Path=$(desktop_quote "$PROJECT_PATH")
Icon=$ICON_EXEC
Terminal=true
Categories=Development;System;
StartupNotify=true
EOF
  chmod +x "$file"
}

write_launcher "$APP_FILE"

# Install a physical desktop icon too, respecting Ubuntu's localized Desktop
# directory when xdg-user-dir is available.
if command -v xdg-user-dir >/dev/null 2>&1; then
  DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
else
  DESKTOP_DIR=""
fi
DESKTOP_DIR="${DESKTOP_DIR:-$HOME/Desktop}"
mkdir -p "$DESKTOP_DIR"
DESKTOP_FILE="$DESKTOP_DIR/zmk-vision.desktop"
cp -f "$APP_FILE" "$DESKTOP_FILE"
chmod +x "$DESKTOP_FILE"
# GNOME may otherwise label a generated .desktop file as untrusted.
if command -v gio >/dev/null 2>&1; then
  gio set "$DESKTOP_FILE" metadata::trusted true >/dev/null 2>&1 || true
fi

printf 'Ярлык создан в меню приложений: %s\n' "$APP_FILE"
printf 'Ярлык на рабочем столе: %s\n' "$DESKTOP_FILE"
printf '%s\n' 'При запуске он проверит обновление, затем поднимет Docker Compose.'
printf 'Проект: %s\n' "$PROJECT_PATH"
