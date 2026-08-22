# ZMK Vision v2.10.1 — Fix worker 503 (auto-provisioned shared worker token)

## Проблема

После запуска `docker compose --profile inference up` в логах API постоянно шло:
```
GET /api/internal/active-model HTTP/1.1" 503 Service Unavailable
```
В inference-worker при этом — **чёрный экран камеры и постоянное «офлайн»**.

**Причина:** внутренний worker API (`/api/internal/*`) требует секрет `ZMK_WORKER_TOKEN`, но при запуске Compose вручную (без установщика) он пуст → API возвращал `503 "Worker API is not configured"`. Воркер не мог получить список камер и активную модель, поэтому не обрабатывал поток и не слал снимки/телеметрию.

## Решение

**Авто-провижининг общего секрета через том `model-data`.**

- API, inference-worker и training-worker монтируют один и тот же `model-data:/models`.
- Если `ZMK_WORKER_TOKEN` в `.env` не задан, создаётся криптостойкий случайный токен (`secrets.token_hex(32)`) и сохраняется в `WORKER_TOKEN_FILE` = `/models/.worker-token`.
- Все три сервиса читают **один и тот же файл**, поэтому всегда согласованы — даже без ручной настройки `.env`.
- Если токен задан вручную (`ZMK_WORKER_TOKEN`) — используется он (файл не создаётся). Приоритет: env → файл.
- Внутренний API теперь требует токен строго (constant-time, `401` при несовпадении) и возвращает `503` **только** если секрет вообще нельзя создать (например, том только для чтения).

Теперь `docker compose --profile inference up` работает **из коробки**.

## Проверки (подтверждено вживую)

- API авто-создаёт `.live-data/models/.worker-token` (64 hex).
- `GET /api/internal/active-model` с правильным токеном → **200** (раньше 503).
- Без токена → **401**, с неверным → **401** (безопасность сохранена).
- Backend **57/57** (добавлены тесты провижининга: генерация, читается один и тот же секрет, приоритет env), установщики/updater/worker **26/26**, Telegram 3/3, MAX 3/3.
- Ruff, Bandit (прод+updater), tsc/lint, `npm audit` (0), pip-audit (0), shell, compose, `git diff --check` — чисто.

## Параметры

```env
# Секрет внутреннего API; пусто = авто-генерация в общий файл
ZMK_WORKER_TOKEN=
# Путь общего файла-секрета (по умолчанию /models/.worker-token)
ZMK_WORKER_TOKEN_FILE=/models/.worker-token
```

Как применить: после обновления выполните `docker compose up -d --build` (все сервисы) и при желании перезапустите inference/training профили. Worker-секрет согласуется автоматически.
