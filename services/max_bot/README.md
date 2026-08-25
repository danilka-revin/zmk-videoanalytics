# ZMK Vision Bot for MAX

Бот для российского мессенджера MAX с ролями admin/operator/viewer, уведомлениями, журналом событий, CSV-отчётами, моделями, hot-swap, порогами и обучением.

1. Создайте бота через `@MasterBot` в MAX и получите token.
2. Узнайте свой числовой MAX user ID.
3. Заполните `MAX_BOT_TOKEN` и `MAX_ADMIN_IDS` в `.env`.
4. Запустите обычный стек: `./start.sh`, затем включите MAX в **Админ → Боты**.

Бот использует long polling. Если ранее был настроен webhook, при запуске polling-подписка удаляется. Для крупного production-контура рекомендуется перевести сервис на HTTPS webhook по официальной документации MAX.

Официальная документация: https://dev.max.ru/docs-api/
