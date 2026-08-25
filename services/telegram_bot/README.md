# Telegram Bot + Mini App

1. Создайте бота через `@BotFather`, получите token.
2. Узнайте Telegram ID через `@userinfobot`.
3. В `.env` задайте `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_IDS` и публичный HTTPS `TELEGRAM_WEBAPP_URL`.
4. Запустите обычный стек: `./start.sh`, затем включите Telegram в **Админ → Боты**.

Роли — whitelist: admin, operator, viewer. Бот работает long polling, внешние inbound-порты не нужны. Mini App требует HTTPS URL, доступный Telegram.
