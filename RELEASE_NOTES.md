# ZMK Vision v1.4.0 — Telegram or MAX Messenger

## Главное
При установке теперь можно выбрать один вариант:

1. Telegram-бот;
2. бот в российском мессенджере MAX;
3. запуск без бота.

Установщики Windows и Linux останавливают прежний bot-сервис и запускают только выбранный профиль. Выбор сохраняется в `MESSENGER_PROVIDER`.

## MAX-бот
Добавлен отдельный сервис `services/max_bot` на `maxapi 1.2.1`:

- whitelist и роли `admin`, `operator`, `viewer`;
- `/status`, `/cameras`, `/events`, `/health`;
- `/logs`, `/report`, `/models`, `/thresholds`;
- `/switch_model`, `/set_threshold`;
- `/train`, `/cancel_training`, `/alert_test`;
- CSV-отчёты;
- автоматические критические уведомления;
- API key и retry временных сетевых ошибок.

Бот создаётся через `@MasterBot` в MAX. Для production официальный MAX API рекомендует HTTPS webhook; текущий сервис использует polling и удаляет старую webhook-подписку перед запуском.

## Docker и установка

- Добавлен Compose profile `max`.
- Добавлены `MAX_BOT_TOKEN`, `MAX_ADMIN_IDS`, `MAX_OPERATOR_IDS`, `MAX_VIEWER_IDS`.
- CI, release pipeline, pip-audit, Ruff, Bandit и Docker build проверяют оба messenger-сервиса.
- Удаление проекта учитывает Telegram и MAX profiles.
- README и подробный beginner guide обновлены для обоих вариантов.

## Запуск вручную

Telegram:

```bash
docker compose --profile telegram up -d --build
```

MAX:

```bash
docker compose --profile max up -d --build
```
