# ZMK Vision v1.3.0 — Final Audit & Stability

Финальный аудит всего проекта: runtime, зависимости, API, модельный pipeline, интерфейс, Telegram, Docker, установщики, CI и релизы.

## Безопасность и зависимости
- FastAPI обновлён до 0.141.1, Starlette до 1.6.0, Uvicorn до 0.52.4.
- Aiogram обновлён до 3.30.0, aiohttp до 3.14.3.
- Удалены неиспользуемые runtime-зависимости и уязвимый `python-multipart`.
- Frontend-зависимости зафиксированы точными версиями; `latest` удалён.
- `pip-audit`, `npm audit`, Ruff и Bandit добавлены как обязательные CI/release gates.
- Прямой API-порт Docker привязан только к `127.0.0.1`; внешний доступ идёт через Nginx.
- OpenAPI получил схему `X-API-Key`, поэтому защищённый API можно проверять через Swagger.
- CORS больше не включает credential mode без необходимости.

## Backend и данные
- Тесты полностью изолированы от preview/production SQLite.
- SQLite WAL включается один раз при старте; добавлены индексы событий, логов и обучения.
- Настройка retention теперь реально удаляет просроченные события и логи.
- `event_cooldown_seconds` реально подавляет покадровый поток повторных тревог.
- Timestamp детекций нормализуется в часовой пояс площадки перед хранением.
- Dashboard получает название и метрики фактически активной модели вместо hardcoded значений.
- Устранены динамические SQL-конструкции, обнаруженные security scanner.
- Rate limiter корректно различает клиентов за доверенным Nginx reverse proxy.

## Frontend и Telegram
- Исправлены отсутствующие React keys и потенциальные console warnings.
- Добавлена обработка ошибок действий, обучения, подтверждения событий и Telegram Mini App.
- Локальный Telegram-бот больше не отправляет недопустимую HTTP-кнопку Mini App; кнопка появляется только для HTTPS.
- Исправлено журналирование исключений Telegram.
- SSE получил Nginx buffering bypass и увеличенный read timeout.

## Docker, установщики и CI
- Runtime и development Python-зависимости разделены.
- Nginx выполняет `nginx -t` во время сборки образа.
- CI и release workflow собирают все Docker-образы, включая Telegram profile.
- Linux installer гарантированно устанавливает `curl`, даже если Docker уже был установлен.
- Добавлены проверки Compose, Nginx, локальной привязки API и release assets.

## Проверки релиза
- Backend, Telegram и installer regression suites.
- Clean virtualenv installation.
- Двойная production-сборка frontend.
- Python compile, Ruff, Bandit, pip-audit и npm audit.
- Windows PowerShell dry-check на `windows-latest`.
- Docker Compose config и сборка всех application images в GitHub Actions.
