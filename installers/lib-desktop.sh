#!/usr/bin/env bash
# =====================================================================
# ZMK Vision — desktop session helpers (Wayland / X11).
#
# Sourced by start.sh and installers/install-linux.sh.
#
# Why this exists
# ---------------
# On a Wayland desktop (Ubuntu 24.04+, Fedora, KDE Plasma Wayland) there is no
# shared X display any more. `xdg-open` only works inside the session that owns
# the compositor: it needs WAYLAND_DISPLAY, XDG_RUNTIME_DIR and
# DBUS_SESSION_BUS_ADDRESS of the logged-in user. When the launcher is started
# with sudo (Docker usually requires it) those variables are stripped or point
# at root's empty session, so `xdg-open` quietly does nothing and the operator
# is left staring at a terminal — the "one click" flow simply never opens the
# panel. The helpers below detect the session and open the URL *as the logged-in
# user* with its own environment.
#
# Opt out with ZMK_NO_OPEN=1 / --no-open (headless servers are detected
# automatically: no WAYLAND_DISPLAY and no DISPLAY means nothing to open).
# =====================================================================

zmk_session_type(){
  if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then echo "wayland"
  elif [[ -n "${DISPLAY:-}" ]]; then echo "x11"
  else echo "headless"; fi
}

zmk_home_of(){
  local user="$1"
  getent passwd "$user" 2>/dev/null | cut -d: -f6
}

# zmk_open_url <url> — best effort, never fatal.
zmk_open_url(){
  local url="${1:-http://localhost:5173}"
  [[ "${ZMK_NO_OPEN:-0}" == "1" || -n "${NONINTERACTIVE:-}" ]] && return 0
  [[ "$(zmk_session_type)" == "headless" ]] && return 0
  command -v xdg-open >/dev/null 2>&1 || return 0

  local run_user="${SUDO_USER:-}"
  # Same session (no sudo): inherit the compositor/DBus environment as-is.
  if [[ -z "$run_user" || "$run_user" == "$(id -un 2>/dev/null || true)" ]]; then
    ( xdg-open "$url" >/dev/null 2>&1 || true ) & disown 2>/dev/null || true
    return 0
  fi

  # Started through sudo: re-run xdg-open as the logged-in user with that
  # user's runtime dir and bus address. This is the case that silently failed
  # on Wayland before.
  local uid runtime dbus home
  uid=$(id -u "$run_user" 2>/dev/null || true)
  [[ -n "$uid" ]] || return 0
  runtime="/run/user/$uid"
  dbus="unix:path=${runtime}/bus"
  home="$(zmk_home_of "$run_user")"
  [[ -d "$runtime" ]] || runtime=""
  [[ -S "${runtime}/bus" ]] || dbus=""
  (
    sudo -u "$run_user" env \
      "HOME=${home:-$runtime}" \
      "XDG_RUNTIME_DIR=$runtime" \
      "DBUS_SESSION_BUS_ADDRESS=$dbus" \
      "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}" \
      "DISPLAY=${DISPLAY:-:0}" \
      "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-wayland}" \
      "XDG_SESSION_DESKTOP=${XDG_SESSION_DESKTOP:-}" \
      "QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-wayland}" \
      xdg-open "$url" >/dev/null 2>&1 || true
  ) & disown 2>/dev/null || true
  return 0
}

# Print a hint that is actually usable on a Wayland desktop: the Flatpak/Snap
# browsers need no extra flags for plain HTTP, but Firefox has no H.264 decoder
# on Ubuntu, so the panel serves VP8 over WebRTC automatically there.
zmk_wayland_hint(){
  [[ "$(zmk_session_type)" == "wayland" ]] || return 0
  echo "[start] Обнаружена сессия Wayland: панель открывается в браузере рабочего стола."
  echo "[start] Если браузер не открылся: откройте http://localhost:5173 вручную"
  echo "[start] (Firefox на Ubuntu показывает поток в VP8 — это нормально, H.264 в нём нет)."
}
