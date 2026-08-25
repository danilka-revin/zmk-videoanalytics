# Telegram Bot + Mini App

1. Создайте бота через `@BotFather`, получите token.
2. Узнайте Telegram ID через `@userinfobot`.
3. Запустите обычный стек: `./start.sh`.
4. В **Админ → Боты** вставьте token, добавьте Telegram ID в «Администраторы ID», укажите публичный HTTPS `Mini App URL` и включите Telegram.

Поле токена write-only: API не возвращает значение, а API-контейнер хранит его в закрытом Docker volume `bot-token-data`, смонтированном в служебные bot-контейнеры только для чтения. Изменение токена перезапускает polling без Docker restart. `TELEGRAM_BOT_TOKEN` и ID в `.env` остаются совместимым запасным вариантом для headless-развёртываний.

Роли — whitelist: admin, operator, viewer. Команда `/cameras` выводит кнопки камер, а `/camera <camera_id>` присылает последний реальный JPEG-кадр от inference-worker. Это снимок, а не поддельный или бесконечный видеопоток. Бот работает long polling, внешние inbound-порты не нужны. Mini App требует HTTPS URL, доступный Telegram.
