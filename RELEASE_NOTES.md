# ZMK Vision v1.2.0 — Themes & Security

## Новое
- Светлая, тёмная и системная темы интерфейса.
- Акцентные цвета, плотность таблиц и компактная боковая панель.
- Панель персонализации с локальным сохранением настроек.
- Рабочее сохранение AI-порогов в разделе настроек.
- Опциональная защита REST API ключом `X-API-Key`.
- Rate limiting для admin/inference, лимит тела запроса и security headers.
- Настраиваемый CORS, persistent SQLite volume и безопасные переменные сервисов.
- Поддержка защищённого API в Telegram-боте.

## Установка
- Windows 10/11: распакуйте ZIP и запустите `installers/install-windows.bat`.
- Ubuntu/Debian: распакуйте TAR.GZ и выполните `bash installers/install-linux.sh`.

Перед production-запуском заполните `.env`, установите `ZMK_API_KEY`, замените пароли PostgreSQL/MinIO и настройте HTTPS.
