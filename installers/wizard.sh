#!/usr/bin/env bash
# =====================================================================
# ZMK Vision — configuration wizard (shared by start.sh and install-linux.sh).
#
# Sourced (not executed) by start.sh and install-linux.sh. Provides
# run_config() which writes .env and .zmk-profiles based on user answers.
#
# Non-interactive: set NONINTERACTIVE=1 and MESSENGER_PROVIDER / ENABLE_*.
# =====================================================================

set_env(){
  local key="$1" value="$2" escaped
  escaped="${value//|/\\|}"
  if grep -q "^${key}=" .env; then sed -i "s|^${key}=.*|${key}=${escaped}|" .env; else printf '%s=%s\n' "$key" "$value" >> .env; fi
}

run_config(){
  [[ -f .env ]] || cp .env.example .env
  chmod 600 .env 2>/dev/null || true

  if [[ -n "${NONINTERACTIVE:-}" ]]; then
    MESSENGER="${MESSENGER_PROVIDER:-none}"
  else
    echo ""
    echo "Выберите мессенджер для уведомлений:"
    echo "  1 — Telegram"
    echo "  2 — MAX"
    echo "  0 — без бота"
    read -r -p "Ваш выбор [0/1/2]: " CHOICE
    case "$CHOICE" in
      1) MESSENGER=telegram;;
      2) MESSENGER=max;;
      0|"") MESSENGER=none;;
      *) echo "Неизвестный вариант: $CHOICE"; return 1;;
    esac
  fi

  PROFILE=()
  case "$MESSENGER" in
    telegram)
      if [[ -n "${NONINTERACTIVE:-}" ]]; then TOKEN="${TELEGRAM_BOT_TOKEN:-}"; ADMIN="${TELEGRAM_ADMIN_IDS:-}"; WEBAPP="${TELEGRAM_WEBAPP_URL:-}"
      else read -r -p "Telegram bot token (от @BotFather): " TOKEN; read -r -p "Ваш Telegram numeric ID (admin): " ADMIN; read -r -p "Public HTTPS Mini App URL (Enter для bot-only): " WEBAPP; fi
      if ! [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then echo "Ошибка: неверный формат Telegram token"; return 1; fi
      if ! [[ "$ADMIN" =~ ^[0-9]+(,[0-9]+)*$ ]]; then echo "Ошибка: Telegram admin ID должен состоять из цифр через запятую"; return 1; fi
      if [[ -n "$WEBAPP" && "$WEBAPP" != https://* ]]; then echo "Ошибка: Mini App URL должен быть https"; return 1; fi
      set_env TELEGRAM_BOT_TOKEN "$TOKEN"; set_env TELEGRAM_ADMIN_IDS "$ADMIN"; set_env TELEGRAM_WEBAPP_URL "$WEBAPP"
      set_env MAX_BOT_TOKEN ""; set_env MESSENGER_PROVIDER "telegram"
      PROFILE=(--profile telegram)
      ;;
    max)
      if [[ -n "${NONINTERACTIVE:-}" ]]; then TOKEN="${MAX_BOT_TOKEN:-}"; ADMIN="${MAX_ADMIN_IDS:-}"
      else read -r -p "MAX bot token от @MasterBot: " TOKEN; read -r -p "Ваш MAX numeric ID (admin): " ADMIN; fi
      if ! [[ "$TOKEN" =~ ^[A-Za-z0-9._:-]{20,500}$ ]]; then echo "Ошибка: неверный формат MAX token"; return 1; fi
      if ! [[ "$ADMIN" =~ ^[0-9]+(,[0-9]+)*$ ]]; then echo "Ошибка: MAX admin ID должен состоять из цифр через запятую"; return 1; fi
      set_env MAX_BOT_TOKEN "$TOKEN"; set_env MAX_ADMIN_IDS "$ADMIN"
      set_env TELEGRAM_BOT_TOKEN ""; set_env MESSENGER_PROVIDER "max"
      PROFILE=(--profile max)
      ;;
    none)
      set_env TELEGRAM_BOT_TOKEN ""; set_env MAX_BOT_TOKEN ""; set_env MESSENGER_PROVIDER "none"
      ;;
    *) echo "MESSENGER_PROVIDER must be telegram, max or none"; return 1;;
  esac

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
    PROFILE+=(--profile inference)
    # Worker token is auto-provisioned by the backend on the shared volume, so
    # we don't need to set it here; but if set explicitly, keep it.
  fi

  if [[ -z "${ZMK_UPDATE_TOKEN:-}" ]]; then
    set_env ZMK_UPDATE_TOKEN "$(tr -d '-' </proc/sys/kernel/random/uuid)"
  fi
  set_env UPDATE_SERVICE_URL "http://updater:8020"

  printf '%s\n' "${PROFILE[@]}" > .zmk-profiles
  echo ""
  echo "Конфигурация сохранена (.env и .zmk-profiles)."
  return 0
}
