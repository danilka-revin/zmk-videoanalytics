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
  # Escape sed replacement metacharacters so an RTSP password containing
  # &, | or a backslash is stored literally rather than corrupting .env.
  local key="$1" value="$2" escaped
  escaped="${value//\\/\\\\}"
  escaped="${escaped//&/\\&}"
  escaped="${escaped//|/\\|}"
  if grep -q "^${key}=" .env; then sed -i "s|^${key}=.*|${key}=${escaped}|" .env; else printf '%s=%s\n' "$key" "$value" >> .env; fi
}

run_config(){
  [[ -f .env ]] || cp .env.example .env
  chmod 600 .env 2>/dev/null || true

  if [[ -n "${NONINTERACTIVE:-}" ]]; then
    MESSENGER="${MESSENGER_PROVIDER:-none}"
  else
    echo ""
    echo "Выберите провайдера для первичной настройки (после запуска оба бота управляются в Admin → Боты):"
    echo "  1 — Telegram"
    echo "  2 — MAX"
    echo "  0 — настрою ботов позже в Admin панели"
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
      if [[ -n "${NONINTERACTIVE:-}" ]]; then TOKEN="${TELEGRAM_BOT_TOKEN:-}"; ADMIN_USERNAME="${TELEGRAM_ADMIN_USERNAMES:-}"; ADMIN_IDS="${TELEGRAM_ADMIN_IDS:-}"; WEBAPP="${TELEGRAM_WEBAPP_URL:-}"
      else read -r -p "Telegram bot token (от @BotFather): " TOKEN; read -r -p "Ваш Telegram username (например @chilavik, несколько через запятую): " ADMIN_USERNAME; ADMIN_IDS=""; read -r -p "Public HTTPS Mini App URL (Enter для bot-only): " WEBAPP; fi
      if ! [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then echo "Ошибка: неверный формат Telegram token"; return 1; fi
      if [[ -n "$ADMIN_USERNAME" ]]; then
        if ! [[ "$ADMIN_USERNAME" =~ ^@[A-Za-z0-9_]{5,32}(,@[A-Za-z0-9_]{5,32})*$ ]]; then echo "Ошибка: Telegram username укажите как @chilavik (несколько — через запятую)"; return 1; fi
        set_env TELEGRAM_ADMIN_USERNAMES "$ADMIN_USERNAME"; set_env TELEGRAM_ADMIN_IDS ""
      elif [[ "$ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
        # Backward compatibility for existing unattended deployments.
        set_env TELEGRAM_ADMIN_IDS "$ADMIN_IDS"
      else
        echo "Ошибка: укажите Telegram username, например @chilavik"; return 1
      fi
      if [[ -n "$WEBAPP" && "$WEBAPP" != https://* ]]; then echo "Ошибка: Mini App URL должен быть https"; return 1; fi
      set_env TELEGRAM_BOT_TOKEN "$TOKEN"; set_env TELEGRAM_WEBAPP_URL "$WEBAPP"
      set_env MESSENGER_PROVIDER "telegram"
      ;;
    max)
      if [[ -n "${NONINTERACTIVE:-}" ]]; then TOKEN="${MAX_BOT_TOKEN:-}"; ADMIN="${MAX_ADMIN_IDS:-}"
      else read -r -p "MAX bot token от @MasterBot (Enter — настроить позже): " TOKEN; read -r -p "Ваш MAX numeric ID (admin, Enter — настроить позже): " ADMIN; fi
      if [[ -n "$TOKEN" && ! "$TOKEN" =~ ^[A-Za-z0-9._:-]{20,500}$ ]]; then echo "Ошибка: неверный формат MAX token"; return 1; fi
      if [[ -n "$ADMIN" && ! "$ADMIN" =~ ^[0-9]+(,[0-9]+)*$ ]]; then echo "Ошибка: MAX admin ID должен состоять из цифр через запятую"; return 1; fi
      set_env MAX_BOT_TOKEN "$TOKEN"; set_env MAX_ADMIN_IDS "$ADMIN"
      set_env MESSENGER_PROVIDER "max"
      if [[ -z "$TOKEN" ]]; then echo "MAX будет запущен в безопасном режиме ожидания. Откройте Admin → Боты, чтобы добавить официальный token позднее."; fi
      ;;
    none)
      # Do not erase existing provider secrets: after the stack is running,
      # enablement and all operational bot settings live in Admin → Боты.
      set_env MESSENGER_PROVIDER "none"
      ;;
    *) echo "MESSENGER_PROVIDER must be telegram, max or none"; return 1;;
  esac

  # Training worker is always started. It reports GPU availability itself and
  # runs on CPU when NVIDIA Container Toolkit is not installed.
  set_env TRAINING_WORKER_URL "http://training-worker:8010"

  if [[ -n "${NONINTERACTIVE:-}" ]]; then INFERENCE="${ENABLE_INFERENCE:-false}"; else read -r -p "Включить реальный RTSP YOLO inference worker? [y/N]: " INFERENCE; fi
  if [[ "$INFERENCE" =~ ^([yY]|true|1)$ ]]; then
    PROFILE+=(--profile inference)
    # Camera configuration is collected once here and saved into .env. The
    # input is hidden so credentials are not echoed into terminal scrollback.
    if [[ -n "${NONINTERACTIVE:-}" ]]; then
      CAMERA_RTSP="${RTSP_CAM_01:-}"
      CAMERA_DEVICE="${INFERENCE_DEVICE:-cpu}"
      CAMERA_TRANSPORT="${RTSP_TRANSPORT:-tcp}"
    else
      echo ""
      echo "Настройка RTSP-камеры (можно оставить пустым и добавить через панель):"
      read -r -s -p "RTSP URL камеры: " CAMERA_RTSP; echo
      read -r -p "Устройство inference [cpu/auto/0, по умолчанию cpu]: " CAMERA_DEVICE
      read -r -p "RTSP transport [tcp/udp/auto, по умолчанию tcp]: " CAMERA_TRANSPORT
      CAMERA_DEVICE="${CAMERA_DEVICE:-cpu}"
      CAMERA_TRANSPORT="${CAMERA_TRANSPORT:-tcp}"
    fi
    if [[ -n "$CAMERA_RTSP" && ! "$CAMERA_RTSP" =~ ^rtsps?://[^[:space:]]+$ ]]; then
      echo "Ошибка: RTSP URL должен начинаться с rtsp:// или rtsps:// и не содержать пробелов"
      return 1
    fi
    case "$CAMERA_DEVICE" in cpu|auto|0|[0-9]) ;; *) echo "Ошибка: INFERENCE_DEVICE должен быть cpu, auto или номер GPU"; return 1;; esac
    case "$CAMERA_TRANSPORT" in tcp|udp|auto) ;; *) echo "Ошибка: RTSP transport должен быть tcp, udp или auto"; return 1;; esac
    [[ -n "$CAMERA_RTSP" ]] && set_env RTSP_CAM_01 "$CAMERA_RTSP"
    set_env INFERENCE_DEVICE "$CAMERA_DEVICE"
    set_env RTSP_TRANSPORT "$CAMERA_TRANSPORT"
    set_env RTSP_TIMEOUT_OPTION "${RTSP_TIMEOUT_OPTION:-timeout}"
    # Worker token is auto-provisioned by the backend on the shared volume.
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
