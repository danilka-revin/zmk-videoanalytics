#!/usr/bin/env bash
# =====================================================================
# ZMK Vision — creates a desktop launcher so the app can be started with a
# SINGLE CLICK on Ubuntu (double-click "ZMK Vision" on the desktop / menu).
#
# Usage:  bash installers/create-desktop.sh [path-to-project]
# Default path: the directory this repo is in.
# =====================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_PATH="${1:-$ROOT}"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/zmk-vision.desktop"
ICON="$HOME/.local/share/icons/zmk-vision.png"

fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ -f "$PROJECT_PATH/start.sh" ]] || fail "start.sh не найден в $PROJECT_PATH"

mkdir -p "$DESKTOP_DIR" "$HOME/.local/share/icons"
# A simple emoji/eye-ish icon: use a plain PNG via python if available, else skip.
if command -v python3 >/dev/null 2>&1; then
  python3 - "$ICON" <<'PY'
import sys
try:
    from PIL import Image, ImageDraw
except Exception:
    sys.exit(0)
try:
    img = Image.new("RGBA", (128, 128), (23, 33, 29, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((20, 24, 108, 104), fill=(213, 255, 69, 255))
    d.ellipse((50, 50, 78, 78), fill=(23, 33, 29, 255))
    img.save(sys.argv[1])
except Exception:
    pass
PY
fi

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=ZMK Vision
Comment=Панель видеоаналитики — запуск и управление
Exec=bash "$PROJECT_PATH/start.sh"
Icon=$ICON
Terminal=true
Categories=Development;System;
StartupNotify=true
EOF
chmod +x "$DESKTOP_FILE"

echo "Создан ярлык: $DESKTOP_FILE"
echo "Теперь можете запустить ZMK Vision двойным кликом по 'ZMK Vision' в меню приложений."
echo "Также:  bash $PROJECT_PATH/start.sh"
