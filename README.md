# ZMK Vision

**ZMK Vision — локальная платформа интеллектуальной видеоаналитики для контроля промышленной безопасности, мониторинга камер и оперативного управления инцидентами.**

Система объединяет RTSP-видеопотоки, AI-детекции, журнал событий, отчёты, администрирование, бота на выбор для Telegram или MAX и мобильное Telegram Mini App в едином on-premise контуре. Проект предназначен для производственных площадок, складов, проходных и опасных зон, где видеоданные и настройки должны оставаться внутри инфраструктуры организации.

> Впервые работаете с Docker и серверными приложениями? Откройте **[подробную инструкцию для начинающих](docs/BEGINNER_GUIDE_RU.md)** — в ней пошагово разобраны Windows, Linux, Telegram, MAX, камеры, безопасность, обновление и типовые ошибки.

![Version](https://img.shields.io/badge/version-2.11.3-25332d)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688)
![React](https://img.shields.io/badge/React-TypeScript-61dafb)
![Docker](https://img.shields.io/badge/deploy-Docker_Compose-2496ed)

## Возможности

### Видеоаналитика и события

- добавление, редактирование и удаление RTSP-камер;
- название, зона, описание, RTSP secret и индивидуальный FPS limit;
- TCP-диагностика камер, фактическая телеметрия и глобальный поиск;
- единый шлюз приёма результатов AI-моделей;
- детекция отсутствия каски и сигнального жилета;
- выявление телефона, курения, неподвижности и входа в опасную зону;
- проверка версии модели, камеры, timestamp, bbox и индивидуального порога confidence;
- идемпотентная доставка по `detection_id` без дублирования событий;
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
- реальный NVIDIA training worker: захват RTSP, псевдоразметка, YOLO11n fine-tuning и ONNX export;
- единичная очередь GPU-задач, отмена обучения и callback прогресса;
- блокировка активации моделей ниже минимальных Precision/Recall;
- регистрация новой версии после завершения обучения;
- ручная активация обученной модели после проверки метрик;
- каталог готовых моделей (`yolo11n`, `yolov8n`, `yolo11s`) и **скачивание в один клик** прямо из формы регистрации.

### Готовые модели (каталог пресетов)

В форме **«Зарегистрировать модель»** есть блок **«Готовые модели — скачать в один клик»**: предобученные веса YOLO (реальные общедоступные файлы с ultralytics), скачиваются и регистрируются в реестре одной кнопкой.

- `GET /api/models/presets` — каталог (имя, формат, число классов, размер, категория, скачана ли).
- `POST /api/models/presets/{id}/download` — скачать, сохранить в `MODEL_DIR` (том `model-data`), вычислить SHA256 и зарегистрировать с `source=preset:<id>`. Повтор — `already:true` (идемпотентно).
- Каталог расширяется env-переменной `ZMK_MODEL_PRESETS_JSON` (массив JSON) под ваши источники.

**Важно (честно):** это **COCO-претрейны**, поэтому после скачивания метрики **не заданы**, и активация откажет (`409`) до тех пор, пока вы не обучите модель на своих данных или не укажете метрики валидации. Они полезны как **отправная точка для дообучения** и для предпросмотра, но **не дадут события безопасности** (нет классов `no_helmet / no_vest / phone_usage / smoking / restricted_zone / immobility`), пока вы не обучите на соответствующих метках.

### Бот на выбор: Telegram или MAX

- Telegram-бот на `aiogram 3` или MAX-бот на `maxapi`;
- установщик запускает только выбранный мессенджер;
- whitelist и разграничение доступа по ролям;
- автоматические уведомления о новых критических событиях;
- состояние системы, камеры, события, логи и CSV-отчёты;
- управление порогами, моделями и задачами обучения;
- Telegram дополнительно поддерживает Mini App по маршруту `/telegram`.

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

Установщик проверит Docker Desktop, создаст `.env`, предложит выбрать Telegram, MAX или запуск без бота и запустит систему.

### Ubuntu / Debian — запуск в один клик

**Одна команда запускает всё:** при первом запуске откроется мастер настройки (выбор мессенджера — Telegram / MAX / «без бота», токены бота, включение inference/training воркеров), затем проект поднимется сам. Повторные запуски — без вопросов.

```bash
./start.sh
```

или `bash start.sh`. Ещё проще — установите ярлык на рабочий стол и запускайте двойным кликом:

```bash
bash installers/create-desktop.sh
```

Дополнительно (всё то же самое, но по шагам):

```bash
# полная установка: зависимости + мастер + запуск
bash installers/install-linux.sh

# только мастер настройки (переспросить бота/воркеры)
bash installers/install-linux.sh --setup

# снова открыть мастер из start.sh
./start.sh --setup

# полностью неинтерактивно (нужны env-переменные)
NONINTERACTIVE=1 MESSENGER_PROVIDER=none ENABLE_INFERENCE=true ./start.sh
```

Скрипт проверит Docker и Compose, при первом запуске спросит конфигурацию (мессенджер, воркеры, токены), сохранит её в `.env` и `.zmk-profiles`, и запустит проект.

### Автообновление

Начиная с `v2.3.0` программа сама обновляет себя при запуске:

```bash
# Linux — одна команда: проверка версии + обновление + запуск
./start.sh
```

```powershell
# Windows
.\start.ps1
```

Лаунчеры и установщики выполняют проверку так:

1. Считывают текущую версию из файла `VERSION`.
2. Запрашивают последний релиз `danilka-revin/zmk-videoanalytics` на GitHub.
3. Если версия новее — скачивают архив, сверяют контрольную сумму **SHA256** и распаковывают.
4. Применяют обновление на место (сохраняя `.env`, `./data`, Docker volumes и базы данных) и сами перезапускаются.
5. Если новая версия не найдена или нет соединения с GitHub — просто запускают систему как обычно.

Отключить автообновление (например, полностью автономный контур):

```bash
ZMK_NO_AUTO_UPDATE=1 ./start.sh
```

```powershell
$env:ZMK_NO_AUTO_UPDATE = "1"; .\start.ps1
```

### Кнопка «Обновить» в панели

С `v2.4.0` в шапке панели есть кнопки **проверки обновления** и **«Обновить до последней версии»**. Они работают через сервис `updater`, который монтирует каталог проекта на хосте и Docker-сокет, поэтому обновление действительно применяется: скачивается архив, сверяется SHA256, новые файлы накладываются на место (сохраняя `.env`, `./data`, базы и профили) и стек пересобирается. Существующие данные и настройки не затрагиваются.

Если сервис `updater` не запущен (например, стенд поднят вручную без него), кнопка честно сообщает, что обновление недоступно, и не выдаёт ложного успеха.

Панельные кнопки проверяют версию через `GET /api/update/status` и запускают обновление через `POST /api/update/apply`.

### Почему карточка камеры чёрная («кадр не получен»)

Картинка в карточке камеры — это последний снимок, который **inference-worker** загружает раз в несколько секунд. Если кадр не поступает, карточка честно показывает причину:

- **«Поток не подключён / RTSP недоступен»** — камера не отдаёт кадр. Проверьте RTSP URL и учётные данные, сеть, и логи:
  ```bash
  docker compose --profile inference logs --tail=100 inference-worker
  ```
- **«Кадр не получен»** — снимков ещё нет: worker не запущен или профиль `inference` не включён.

Для просмотра кадров нужен запущенный `inference-worker`. Убедитесь, что он запущен (установщик включает профиль `inference`, либо запустите вручную):
```bash
docker compose --profile inference up -d --build
```

При наличии кадра на карточке отображается его возраст («только что», «N сек назад»). Диагностика показывает состояние `fresh / stale / none` и возраст кадра по каждой камере.

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

## Запуск с MAX-ботом

1. Создайте бота через `@MasterBot` в MAX.
2. Заполните `MAX_BOT_TOKEN` и `MAX_ADMIN_IDS` в `.env`.
3. Запустите только MAX-профиль:

```bash
docker compose --profile max up -d --build
```

Для переключения остановите прежний bot-сервис и запустите нужный профиль. Установщики Windows/Linux делают это автоматически и не запускают два бота одновременно.

Подробнее: [`services/max_bot/README.md`](services/max_bot/README.md).

### Команды ботов Telegram и MAX

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
Web Admin ── Telegram/MAX Bot ── Mini App ── СКУД Webhook
```

После первого запуска реестр камер, событий, пользователей и моделей пуст. Камеры добавляются через Web/API, телеметрия поступает от ingestion worker, а детекции — через канонический inference gateway. Система не подставляет демонстрационные FPS, GPU, Precision/Recall или события.

Подробнее:

- [Подробная инструкция для начинающих](docs/BEGINNER_GUIDE_RU.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Передача данных от модели](docs/MODEL_DATA_FLOW.md)
- [Автодообучение](docs/AUTO_TRAINING.md)
- [Telegram](services/telegram_bot/README.md)
- [MAX](services/max_bot/README.md)

## Основные API

| Метод | Endpoint | Назначение |
|---|---|---|
| `GET` | `/api/dashboard` | KPI и динамика |
| `POST` | `/api/inference/detections` | Детекции модели → события |
| `GET/POST` | `/api/cameras` | Список и добавление камер |
| `GET/PUT/DELETE` | `/api/cameras/{id}` | Карточка, редактирование и удаление |
| `POST` | `/api/cameras/{id}/diagnostics` | Проверка RTSP host/port |
| `POST` | `/api/cameras/{id}/telemetry` | Фактические FPS/status/latency от worker |
| `GET` | `/api/diagnostics` | Диагностика системы и всех камер |
| `GET` | `/api/search` | Глобальный поиск |
| `GET` | `/api/capabilities` | Подключённые возможности и workers |
| `GET` | `/api/update/status` | Текущая и последняя версия, наличие обновления |
| `POST` | `/api/update/apply` | Применить обновление через сервис `updater` |
| `GET` | `/api/events` | Журнал событий |
| `POST` | `/api/events/{id}/ack` | Подтверждение события |
| `GET` | `/api/models` | Реестр моделей |
| `GET` | `/api/models/presets` | Каталог готовых моделей |
| `POST` | `/api/models/presets/{id}/download` | Скачать и зарегистрировать готовую модель |
| `POST` | `/api/models/{name}/activate` | Атомарный hot-swap |
| `GET/POST` | `/api/training/jobs` | Задачи автодообучения (source: `camera`/`dataset`) |
| `GET/POST/DELETE` | `/api/datasets` | Загрузка/список/удаление фото-, видео- или YOLO-датасетов |
| `GET/PUT` | `/api/admin/config` | Конфигурация платформы |
| `GET/POST` | `/api/admin/users` | Пользователи и роли |
| `GET` | `/api/reports/errors` | Отчёт по ошибкам |
| `GET` | `/api/reports/events.csv` | CSV событий |
| `GET` | `/api/system-health` | Состояние ресурсов |

## Реальное обучение на NVIDIA GPU

Установите NVIDIA Driver и NVIDIA Container Toolkit, затем запустите:

```bash
# в .env: TRAINING_WORKER_URL=http://training-worker:8010
# CPU fallback (запускается везде)
docker compose --profile training up -d --build

# NVIDIA Container Toolkit установлен
docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile training up -d --build
```

Базовый Compose больше не требует NVIDIA runtime и не падает на компьютерах без GPU. Установщик сам подключает `docker-compose.gpu.yml`, только если Docker сообщает о runtime `nvidia`.

При ручном запуске из панели worker захватывает кадры выбранной RTSP-камеры, создаёт псевдоразметку активной моделью (или YOLO11n), обучает YOLO11n, экспортирует ONNX в persistent `model-data` и регистрирует модель через API. Если получено меньше 10 размеченных кадров, задача завершается ошибкой — пустые метрики не генерируются.

### Обучение на готовом датасете

Помимо кадров с камеры, можно обучить модель на **загруженном архиве**. Тип определяется автоматически:

- **Папка фоток** (`.jpg/.png/.webp/...`) — worker авторазмечает каждое фото активной моделью (или YOLO11n) и обучает.
- **Пачка видео** (`.mp4/.avi/.mov/.mkv/...`) — worker нарезает кадры, авторазмечает и обучает.
- **YOLO-датасет** (`data.yaml` + `images/` + `labels/`) — обучение без авторазметки.

На вкладке **Модели** переключите источник на **«Готовый датасет»**, загрузите `.zip`, выберите его и нажмите «Запустить». API: `POST /api/datasets`, `GET /api/datasets`, `DELETE /api/datasets/{id}`, параметр `source=dataset` (+ `dataset_kind: yolo|images|videos`) у `POST /api/training/jobs`. Данные хранятся в volume `dataset-data`, общем для API и training worker.

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
pip install -r backend/requirements-dev.txt
PYTHONPATH=backend pytest backend/tests -q
```

Боты Telegram и MAX:

```bash
pip install -r services/telegram_bot/requirements.txt -r services/max_bot/requirements.txt
PYTHONPATH=services/telegram_bot pytest services/telegram_bot/test_helpers.py -q
PYTHONPATH=services/max_bot pytest services/max_bot/test_helpers.py -q
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
services/max_bot/        MAX-бот и push-оповещения
services/updater/        Сервис самостоятельного обновления из панели
installers/              Установщики и автообновление Windows и Linux
docs/                    Архитектура и контракты
docker-compose.yml       Локальная и production-конфигурация
.github/workflows/       CI и автоматическая публикация релизов
```

## Статус

Версия `2.11.3` является рабочим интеграционным контуром без витринных данных. Web-панель, REST API, camera CRUD, диагностика, поиск, отчёты, Telegram/MAX и внешний inference gateway работают локально. Для распознавания подключите RTSP ingestion worker и зарегистрируйте реальный артефакт модели. Обучение включается Compose-профилем `training` и требует NVIDIA Driver/Container Toolkit; без доступной CUDA кнопка запуска остаётся недоступной.

## Защита API и эксплуатационная безопасность

Для изолированной демонстрации API запускается без ключа. В production задайте длинный случайный ключ:

```env
ZMK_API_KEY=replace-with-at-least-32-random-characters
CORS_ORIGINS=https://vision.example.ru
RATE_LIMIT_PER_MINUTE=120
```

Web-панель принимает ключ в меню **Персонализация → Защищённый API** и хранит его только в `localStorage`. Telegram/MAX-сервисы получают тот же ключ из окружения. API применяет constant-time проверку ключа, лимит размера запроса, rate limiting для admin/inference, запрет кэширования и защитные HTTP-заголовки.

SQLite размещается в Docker volume и работает в WAL-режиме с foreign keys и busy timeout. Пароли инфраструктурных PostgreSQL и MinIO задаются только через `.env` и должны быть заменены перед production-запуском. Telegram Mini App проходит HMAC-проверку `initData` и получает права только из whitelist ролей.

## Безопасность

Не публикуйте `.env`, токены Telegram/MAX, RTSP URL с паролями, приватные SSH-ключи и production credentials. Для обнаруженных уязвимостей используйте приватный канал владельца репозитория, а не публичный Issue.
