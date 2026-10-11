#!/usr/bin/env bash
# =====================================================================
# Zovod → домашний сервер (ноутбук/ПК с Debian): одноразовая доводка.
#
#   sudo bash setup-home.sh
#
# Что делает (всё, что можно сделать на самом ноутбуке):
#   1) ставит Docker + Compose, включает автозапуск Docker при загрузке;
#   2) запрещает ноутбуку засыпать и выключаться при закрытии крышки;
#   3) освобождает порты 80/443 (nginx/apache заменяет Caddy из стека);
#   4) открывает файрвол (если установлен ufw): 22, 80, 443, 8555 TCP+UDP;
#   5) показывает локальный IP (для проброса портов на роутере),
#      внешний IP (для DNS-записей на reg.ru) и предупреждает про CGNAT;
#   6) проверяет DNS домена и ответ сайта.
#
# Что скрипт сделать НЕ может (5 минут руками, см. docs/HOME_SERVER_RU.md):
#   • проброс портов 80, 443, 8555 (TCP+UDP) на роутере → на этот ноутбук;
#   • A-записи @ и www на reg.ru → ваш внешний IP.
# =====================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ "${EUID}" -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "Запустите от root: sudo bash setup-home.sh" >&2; exit 1; }
  exec sudo bash "$0" "$@"
fi

DOMAIN=""
if [[ -f deploy.env ]]; then
  DOMAIN="$(sed -n 's/^ZMK_DOMAIN=//p' deploy.env | tail -1 | tr -d '[:space:]')"
fi
DOMAIN="${DOMAIN:-maisaak-3dprintlab.online}"

echo "== 1. Docker =="
if ! command -v docker >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15
  apt-get install -y ca-certificates curl docker.io
  apt-get install -y docker-compose-v2 || apt-get install -y docker-compose-plugin
fi
systemctl enable --now docker
echo "   Docker установлен и будет стартовать при каждой загрузке."
echo "   (контейнеры стека помечены restart: unless-stopped — после"
echo "   перезагрузки ноутбука сайт поднимется сам, без команд)"

echo "== 2. Ноутбук не должен засыпать =="
# Крышку можно закрывать — сервер продолжит работать.
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/zmk-server.conf <<'EOF'
# Zovod home server: ноутбук работает как сервер — не спать.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
# Применение настроек logind. На десктопных Debian перезапуск logind может
# на секунду мигнуть сессией — это нормально; надёжнее всего перезагрузиться.
systemctl restart systemd-logind 2>/dev/null || true
echo "   Сон/гибернация отключены, крышку можно закрывать."
echo "   Совет: в BIOS включите «Restore on AC Power Loss» — ноут сам"
echo "   включится после отключения электричества (если без батареи)."

echo "== 3. Порты 80/443 свободны =="
if systemctl disable --now nginx 2>/dev/null; then echo "   nginx отключён (его заменяет Caddy)"; fi
if systemctl disable --now apache2 2>/dev/null; then echo "   apache2 отключён (его заменяет Caddy)"; fi
echo "   ок"

echo "== 4. Файрвол =="
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 443/udp >/dev/null
  ufw allow 8555/tcp >/dev/null
  ufw allow 8555/udp >/dev/null
  ufw --force enable >/dev/null
  ufw status | sed 's/^/   /'
else
  echo "   ufw не установлен — на чистом Debian файрвола нет, порты открыты. Ок."
fi

echo "== 5. Адреса для роутера и DNS =="
LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
PUB_IP="$(curl -4 -fsS -m 10 https://api.ipify.org 2>/dev/null || curl -4 -fsS -m 10 https://ifconfig.me 2>/dev/null || true)"
echo "   Локальный IP ноутбука (для проброса портов на роутере): ${LAN_IP:-не определён}"
echo "   Внешний IP (для A-записей @ и www на reg.ru):           ${PUB_IP:-не определён}"
echo
echo "   На роутере пробросьте на ${LAN_IP:-<IP ноутбука>}:"
echo "     80  TCP        → ${LAN_IP:-<IP ноутбука>}:80"
echo "     443 TCP и UDP  → ${LAN_IP:-<IP ноутбука>}:443"
echo "     8555 TCP и UDP → ${LAN_IP:-<IP ноутбука>}:8555   (живое видео камер)"
echo "   И закрепите за ноутбуком этот IP (DHCP reservation / статический IP)."
echo
echo "   ВАЖНО (CGNAT): зайдите в веб-панель роутера и посмотрите его WAN-IP."
echo "   Если WAN-IP роутера НЕ совпадает с внешним IP выше (или начинается"
echo "   на 100.64–100.127 / 10. / 172.16–31 / 192.168) — вы за CGNAT"
echo "   провайдера, и порты снаружи не откроются. Решение: заказать у"
echo "   провайдера услугу «белый (публичный) IP» — обычно 100–200 ₽/мес."

echo "== 6. Проверка DNS и сайта =="
DNS_IP="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"
echo "   DNS: $DOMAIN → ${DNS_IP:-не найден (добавьте A-записи на reg.ru и подождите)}"
if [[ -n "${DNS_IP:-}" && -n "${PUB_IP:-}" && "$DNS_IP" != "$PUB_IP" ]]; then
  echo "   ВНИМАНИЕ: DNS указывает на $DNS_IP, а ваш внешний IP — $PUB_IP."
  echo "   Обновите A-записи @ и www на reg.ru → $PUB_IP (обновление до часа)."
fi
if command -v curl >/dev/null 2>&1; then
  curl -sS -o /dev/null -m 15 -w "   http://$DOMAIN → HTTP %{http_code}\n" "http://$DOMAIN" || true
  curl -sS -o /dev/null -m 15 -w "   https://$DOMAIN/api/health → HTTP %{http_code}\n" "https://$DOMAIN/api/health" || true
fi

echo
echo "Готово. Дальше:"
echo "  1) ./start.sh --no-open        — собрать и запустить стек (5–20 минут)"
echo "  2) роутер + DNS по подсказкам выше (если ещё не сделаны)"
echo "  3) открыть https://www.$DOMAIN  → вход admin / 72327232"
echo "Если сертификат не выпустился сразу: docker compose restart caddy"
