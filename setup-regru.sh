#!/usr/bin/env bash
# =====================================================================
# Zovod → VPS reg.ru: одноразовая доводка сервера под домен.
#
#   bash setup-regru.sh
#
# Что делает:
#   1) освобождает порты 80/443 (отключает nginx/apache — их заменяет Caddy);
#   2) открывает файрвол: 22, 80, 443, 8555 (TCP и UDP);
#   3) ставит пароль 72327232 всем панельным пользователям;
#   4) проверяет, что сайт отвечает по домену.
# =====================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DOMAIN=""
if [[ -f deploy.env ]]; then
  DOMAIN="$(sed -n 's/^ZMK_DOMAIN=//p' deploy.env | tail -1 | tr -d '[:space:]')"
fi
DOMAIN="${DOMAIN:-maisaak-3dprintlab.online}"

echo "== 1. Порты 80/443 =="
if systemctl disable --now nginx 2>/dev/null; then echo "   nginx отключён"; fi
if systemctl disable --now apache2 2>/dev/null; then echo "   apache2 отключён"; fi
echo "   ок"

echo "== 2. Файрвол =="
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 8555/tcp >/dev/null
  ufw allow 8555/udp >/dev/null
  ufw --force enable >/dev/null
  ufw status | sed 's/^/   /'
else
  echo "   ufw не найден — откройте порты 22, 80, 443, 8555 (TCP+UDP) в панели reg.ru."
fi

echo "== 3. Пароли панели (все пользователи) =="
bash "$ROOT/set-passwords.sh" 72327232 || true

echo "== 4. Проверка =="
DNS_IP="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"
echo "   DNS: $DOMAIN → ${DNS_IP:-не найден (ждём обновления DNS)}"
if command -v curl >/dev/null 2>&1; then
  curl -sS -o /dev/null -m 15 -w "   http://$DOMAIN → HTTP %{http_code}\n" "http://$DOMAIN" || true
  curl -sS -o /dev/null -m 15 -w "   https://$DOMAIN/api/health → HTTP %{http_code}\n" "https://$DOMAIN/api/health" || true
fi

echo
echo "Готово. Вход: admin / 72327232 → https://$DOMAIN"
echo "Если сертификат ещё не выпустился: docker compose restart caddy"
echo "Не забудьте в панели reg.ru у VPS открыть порты 80, 443 и 8555 (TCP+UDP)."
