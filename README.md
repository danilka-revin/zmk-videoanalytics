# ZMK Vision

**ZMK Vision — локальная платформа интеллектуальной видеоаналитики для контроля промышленной безопасности, мониторинга камер и оперативного управления инцидентами.**

Система объединяет RTSP-видеопотоки, AI-детекции, журнал событий, отчёты, администрирование, Telegram-бота и мобильное Telegram Mini App в едином on-premise контуре. Проект предназначен для производственных площадок, складов, проходных и опасных зон, где видеоданные и настройки должны оставаться внутри инфраструктуры организации.

> Впервые работаете с Docker и серверными приложениями? Откройте **[подробную инструкцию для начинающих](docs/BEGINNER_GUIDE_RU.md)** — в ней пошагово разобраны Windows, Linux, Telegram, камеры, безопасность, обновление и типовые ошибки.

![Version](https://img.shields.io/badge/version-1.2.3-25332d)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab)
![FastAPI](https://img.shields.io/badge/FastAPI-0.116-009688)
![React](https://img.shields.io/badge/React-TypeScript-61dafb)
![Docker](https://img.shields.io/badge/deploy-Docker_Compose-2496ed)

## Возможности

### Видеоаналитика и события

- управление десятью и более RTSP-камерами;
- единый шлюз приёма результатов AI-моделей;
- детекция отсутствия каски и сигнального жилета;
- выявление телефона, курения, неподвижности и входа в опасную зону;
- проверка версии модели, камеры и порога confidence для каждой детекции;
- классификация критичности и защита от результатов устаревшей модели;
- подтверждение инцидентов оператором и журнал действий.

### Панель управления

- светлая, тёмная и системная темы;
- четыре акцентных цвета;
- комфортная и компактная плотность;
- полноразмерная и компактная боковая панель;
- сохранение персонализации в браузере;

- состояние камер и сервисов в реальном времени;
- KPI, FPS, задержка, GPU, Precision и Recall;
- график событий и список критических инцидентов;
- управление камерами, AI-порогами, архивом и интеграциями;
- пользователи и роли `admin`, `operator`, `viewer`;
- отчёты по событиям и системным ошибкам;
- экспорт CSV.

### Управление моделями

- реестр версий моделей;
- атомарная горячая замена без остановки API;
- audit log переключений;
- запуск автодообучения по кадрам выбранной камеры;
- этапы захвата, quality gate, псевдоразметки, train/validation и экспорта ONNX;
- регистрация новой версии после завершения обучения;
- ручная активация обученной модели после проверки метрик.

### Telegram

- Telegram-бот на `aiogram 3`;
- whitelist и разграничение доступа по ролям;
- автоматические уведомления о новых критических событиях;
- состояние системы, камеры, события, логи и отчёты;
- управление порогами, моделями и задачами обучения;
- мобильное Telegram Mini App по маршруту `/telegram`.

## Интерфейсы

| Интерфейс | Адрес после локального запуска |
|---|---|
| Web-панель | http://localhost:5173 |
| Telegram Mini App | http://localhost:5173/telegram |
| REST API | http://localhost:8000 |
| OpenAPI / Swagger | http://localhost:8000/docs |
| Health check | http://localhost:8000/api/health |

## Быстрый запуск

### Docker Compose

```bash
git clone https://github.com/danilka-revin/zmk-videoanalytics.git
cd zmk-videoanalytics
cp .env.example .env
docker compose up --build
```

### Windows 10/11

Скачайте ZIP из [GitHub Releases](https://github.com/danilka-revin/zmk-videoanalytics/releases), распакуйте его и запустите:

```text
installers\install-windows.bat
```

Установщик проверит Docker Desktop, создаст `.env`, предложит настроить Telegram и запустит систему.

### Ubuntu / Debian

```bash
bash installers/install-linux.sh
```

Скрипт проверит Docker и Compose, установит недостающие компоненты и запустит проект.

## Запуск с Telegram-ботом

1. Создайте бота через `@BotFather`.
2. Скопируйте `.env.example` в `.env`.
3. Заполните параметры:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ADMIN_IDS=123456789
TELEGRAM_OPERATOR_IDS=
TELEGRAM_VIEWER_IDS=
TELEGRAM_WEBAPP_URL=https://vision.example.ru/telegram
```

4. Запустите сервисы:

```bash
docker compose --profile telegram up -d --build
```

Telegram Mini App требует публичный HTTPS-адрес. Токены, RTSP-пароли и другие секреты не должны попадать в Git.

### Команды бота

| Команда | Назначение | Минимальная роль |
|---|---|---|
| `/status` | Состояние платформы | viewer |
| `/cameras` | Список камер | viewer |
| `/events` | Последние события | viewer |
| `/health` | CPU, RAM, GPU и сервисы | viewer |
| `/logs` | Ошибки и предупреждения | operator |
| `/report` | CSV-отчёт | operator |
| `/models` | Реестр моделей | viewer |
| `/thresholds` | Пороги AI | operator |
| `/switch_model <name>` | Горячая замена | admin |
| `/set_threshold <metric> <value>` | Изменение порога | admin |
| `/train <camera_id>` | Запуск дообучения | admin |
| `/users` | Пользователи | admin |
| `/alert_test` | Тест оповещения | admin |

## Архитектура

```text
RTSP-камеры
    ↓
Video Ingestion / GStreamer
    ↓
Redis Streams
    ↓
YOLO / TensorRT Inference Worker
    ↓
Inference Gateway → Event Processing
    ↓
PostgreSQL / TimescaleDB ── MinIO Archive
    ↓
Web Admin ── Telegram Bot ── Mini App ── СКУД Webhook
```

Текущая версия запускается без GPU и реальных камер благодаря демонстрационному генератору. Канонический API-контракт уже проверяет модель, камеру, confidence и записывает принятые детекции в журнал. Для промышленного контура к нему подключается отдельный RTSP/inference worker.

Подробнее:

- [Подробная инструкция для начинающих](docs/BEGINNER_GUIDE_RU.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Передача данных от модели](docs/MODEL_DATA_FLOW.md)
- [Автодообучение](docs/AUTO_TRAINING.md)
- [Telegram](services/telegram_bot/README.md)

## Основные API

| Метод | Endpoint | Назначение |
|---|---|---|
| `GET` | `/api/dashboard` | KPI и динамика |
| `POST` | `/api/inference/detections` | Детекции модели → события |
| `GET/POST` | `/api/cameras` | Управление камерами |
| `GET` | `/api/events` | Журнал событий |
| `POST` | `/api/events/{id}/ack` | Подтверждение события |
| `GET` | `/api/models` | Реестр моделей |
| `POST` | `/api/models/{name}/activate` | Атомарный hot-swap |
| `GET/POST` | `/api/training/jobs` | Задачи автодообучения |
| `GET/PUT` | `/api/admin/config` | Конфигурация платформы |
| `GET/POST` | `/api/admin/users` | Пользователи и роли |
| `GET` | `/api/reports/errors` | Отчёт по ошибкам |
| `GET` | `/api/reports/events.csv` | CSV событий |
| `GET` | `/api/system-health` | Состояние ресурсов |

## Production-профиль

```bash
docker compose --profile production up -d
```

Production-профиль поднимает инфраструктурные контейнеры PostgreSQL, Redis и MinIO для следующего этапа интеграции. Текущая версия API использует persistent SQLite и не выдаёт PostgreSQL/Redis/MinIO за подключённые хранилища. Перед вводом в эксплуатацию необходимо реализовать и проверить миграцию адаптеров данных, а также:

- заменить все пароли и токены;
- включить HTTPS и reverse proxy;
- хранить RTSP credentials в secrets;
- настроить резервное копирование;
- подключить реальные модели и GPU worker;
- провести валидацию Precision/Recall на данных площадки;
- настроить сетевые политики и срок хранения архива.

## Разработка и тестирование

Backend:

```bash
pip install -r backend/requirements.txt
PYTHONPATH=backend pytest backend/tests -q
```

Telegram-бот:

```bash
pip install -r services/telegram_bot/requirements.txt
PYTHONPATH=services/telegram_bot pytest services/telegram_bot/test_helpers.py -q
```

Frontend:

```bash
cd frontend
npm ci
npm run lint
npm run build
```

Проверки автоматически выполняются в GitHub Actions для push и pull request. Теги вида `v*` запускают сборку GitHub Release с ZIP/TAR.GZ и установщиками для Windows и Linux.

## Структура репозитория

```text
backend/                 FastAPI, SQLite, события, модели и отчёты
frontend/                React/TypeScript Web UI и Telegram Mini App
services/telegram_bot/   Telegram-бот и push-оповещения
installers/              Установщики Windows и Linux
docs/                    Архитектура и контракты
docker-compose.yml       Локальная и production-конфигурация
.github/workflows/       CI и автоматическая публикация релизов
```

## Статус

Версия `1.2.3` является рабочим демонстрационным и интеграционным контуром. Web-панель, REST API, администрирование, отчёты, Telegram-интерфейсы, hot-swap и жизненный цикл обучения работают локально. Реальное распознавание требует подключения камер, GPU, весов моделей и размеченного набора данных.

## Защита API и эксплуатационная безопасность

Для изолированной демонстрации API запускается без ключа. В production задайте длинный случайный ключ:

```env
ZMK_API_KEY=replace-with-at-least-32-random-characters
CORS_ORIGINS=https://vision.example.ru
RATE_LIMIT_PER_MINUTE=120
```

Web-панель принимает ключ в меню **Персонализация → Защищённый API** и хранит его только в `localStorage`. Telegram-сервис получает тот же ключ из окружения. API применяет constant-time проверку ключа, лимит размера запроса, rate limiting для admin/inference, запрет кэширования и защитные HTTP-заголовки.

SQLite размещается в Docker volume и работает в WAL-режиме с foreign keys и busy timeout. Пароли инфраструктурных PostgreSQL и MinIO задаются только через `.env` и должны быть заменены перед production-запуском. Telegram Mini App проходит HMAC-проверку `initData` и получает права только из whitelist ролей.

## Безопасность

Не публикуйте `.env`, токены Telegram, RTSP URL с паролями, приватные SSH-ключи и production credentials. Для обнаруженных уязвимостей используйте приватный канал владельца репозитория, а не публичный Issue.
