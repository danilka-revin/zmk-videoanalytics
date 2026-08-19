# ZMK Vision v1.2.2 — Reliability & Installer Fixes

Исправляющий релиз, направленный на предсказуемую установку, безопасность данных и устойчивость интеграций.

## Исправления
- Windows PowerShell installer теперь обязательно проходит реальный parse/dry-check на `windows-latest` до публикации релиза.
- Установщики Windows и Linux получили dry-check, проверку полного архива, валидацию Telegram-параметров, Compose config и ожидание health checks.
- Установщик выводит логи сервиса при неуспешном запуске и не сообщает об успехе до готовности API и Web.
- Production Web переведён с Vite dev server на multi-stage Nginx image.
- Добавлены SPA fallback, API reverse proxy, CSP и отдельная политика iframe для Telegram Mini App.
- Telegram Mini App теперь передаёт подписанный `initData`; backend проверяет HMAC, срок и whitelist/RBAC.
- RTSP credentials скрыты из API-ответов, входные URL и timestamps валидируются.
- CSV export защищён от spreadsheet formula injection.
- SQLite включает WAL, foreign keys, busy timeout и persistent volume.
- Прерванные задачи обучения получают статус failed; коллизии имён моделей отклоняются.
- Telegram HTML экранируется, GET-запросы к API повторяются при временных сетевых ошибках.
- Удалена внешняя зависимость интерфейса от Google Fonts.
- Исправлены загрузки CSV при включённом API key.
- Повреждённые настройки localStorage больше не блокируют запуск интерфейса.

## Установка
- Windows 10/11: распакуйте полный ZIP и запустите `installers/install-windows.bat`.
- Ubuntu/Debian: распакуйте полный TAR.GZ и выполните `bash installers/install-linux.sh`.
- Проверка без установки: `bash installers/install-linux.sh --check` или `install-windows.bat -CheckOnly`.
