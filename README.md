# ZMK Vision

Рабочий MVP on-premise платформы видеоаналитики промышленной безопасности по требованиям из ТЗ: диспетчерская панель, 10 RTSP-камер, события СИЗ и опасного поведения, мониторинг, hot-swap моделей, журнал и CSV-отчёт.

![status](https://img.shields.io/badge/status-MVP-d5ff45) ![api](https://img.shields.io/badge/API-FastAPI-009688) ![ui](https://img.shields.io/badge/UI-React_+_TypeScript-61dafb)

## Что уже работает

- Панель состояния 10 камер, KPI, GPU, Precision/Recall, задержка и график событий.
- Реальный REST API на FastAPI с OpenAPI (`/docs`), SQLite и начальными данными.
- Журнал событий с фильтрами API, подтверждением оператором и генератором тестового события.
- Полноценная админ-панель: общие параметры, AI/GPU и пороги, архив MinIO, Telegram, webhook СКУД, RTSP, пользователи/RBAC, аудит и отчёт по WARNING/ERROR/CRITICAL.
- Проверяемый шлюз данных модели `POST /api/inference/detections`: версия активной модели, камера, confidence, severity, запись события и причины отклонения.
- Атомарная горячая замена модели без остановки API; проверка реестра и audit log переключения.
- Автодообучение по кадрам выбранной online-камеры: захват, quality gate, псевдоразметка, train/val, fine-tuning, валидация и регистрация новой версии.
- Управление камерами и порогами детекции.
- Состояние сервисов, структурированные данные, SSE `/api/stream`, выгрузка CSV.
- Адаптивная React/TypeScript-панель на русском языке.
- Docker Compose, healthcheck, CI; production-профиль с PostgreSQL, Redis и MinIO.

> AI/RTSP слой в MVP работает через детерминированный симулятор. API-контракт готов для подключения GStreamer/FFmpeg и inference worker на YOLO/TensorRT. Веса моделей и реальные RTSP URL намеренно не входят в репозиторий.

## Быстрый запуск

### Docker
```bash
docker compose up --build
```
Панель: http://localhost:5173 · API: http://localhost:8000/docs

### Локально
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
PYTHONPATH=backend uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
В другом терминале:
```bash
cd frontend && npm install && npm run dev
```

## Основные API

| Метод | URL | Назначение |
|---|---|---|
| GET | `/api/dashboard` | KPI и тренд |
| POST | `/api/inference/detections` | Детекции модели → события системы |
| GET/PUT | `/api/admin/config` | Полная конфигурация |
| GET/POST | `/api/admin/users` | Пользователи и RBAC |
| GET/POST | `/api/cameras` | Камеры |
| GET | `/api/events` | События и фильтры |
| POST | `/api/events/{id}/ack` | Подтверждение |
| POST | `/api/events/simulate` | Тестовая детекция |
| GET | `/api/models` | Реестр моделей |
| POST | `/api/models/{name}/activate` | Атомарный hot-swap |
| POST/GET | `/api/training/jobs` | Автодообучение и прогресс |
| GET | `/api/admin/summary` | Сводка админ-панели |
| GET | `/api/reports/errors` | Отчёт по ошибкам |
| GET | `/api/reports/errors.csv` | Экспорт логов ошибок |
| GET | `/api/system-health` | CPU/GPU/сервисы |
| GET | `/api/reports/events.csv` | Выгрузка |

## Production-профиль
```bash
docker compose --profile production up -d
```
Перед использованием замените секреты, включите HTTPS/reverse proxy и вынесите RTSP credentials в secrets. Для реального внедрения следующие этапы: inference worker, Redis Streams, S3-архивация JPEG/MP4, Telegram-бот, RBAC/JWT и интеграция со СКУД.

## Тесты
```bash
pip install -r backend/requirements.txt
PYTHONPATH=backend pytest backend/tests -q
cd frontend && npm run build
```

Архитектура и ограничения описаны в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
