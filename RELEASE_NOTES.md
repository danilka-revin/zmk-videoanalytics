# ZMK Vision v2.1.0 — Real NVIDIA Auto-Training

## Реальное автодообучение
- NVIDIA GPU worker на Ultralytics 8.4.126.
- RTSP capture с FPS limit.
- Псевдоразметка активной моделью или YOLO11n.
- Раздельные train/validation наборы.
- YOLO11n fine-tuning и динамический ONNX export.
- Persistent model/data volumes, progress callback и cancellation.
- Задача завершается ошибкой при недостатке кадров или разметки — успешные метрики не подставляются.

## Убраны витринные данные
- Чистая установка стартует без камер, событий, пользователей и моделей.
- Удалены фиктивные GPU/CPU/RAM/Disk, FPS, latency и модельные метрики.
- Ресурсы сервера измеряются через psutil и NVIDIA NVML; при отсутствии GPU показывается `—`.
- Генераторы тестовых событий и ошибок недоступны в production.
- Симуляция обучения удалена и добавлен реальный NVIDIA GPU training worker: RTSP capture, pseudo-labeling, YOLO11n fine-tuning, ONNX export, progress callbacks и cancellation.
- Dashboard отображает только фактически зарегистрированную модель и полученную телеметрию.

## Полное управление камерами
- Создание, просмотр, редактирование и удаление камер.
- Название, зона, описание, RTSP URL, enabled и индивидуальный FPS limit.
- RTSP secret не возвращается в браузер после сохранения.
- Удаление камеры с событиями требует явного `delete_events=true`.
- Endpoint телеметрии ingestion worker: status, фактический FPS и latency.
- TCP-диагностика одной камеры и параллельная диагностика всех камер.

## Реальные функции интерфейса
- Рабочий глобальный поиск по камерам, событиям и моделям.
- Рабочая кнопка диагностики и отдельный раздел диагностики.
- Рабочие кнопки добавления, редактирования, проверки и удаления камер.
- Регистрация внешнего ONNX/TensorRT/PyTorch артефакта с метриками, URI и checksum.
- Пустые состояния вместо вымышленных показателей.
- Исправлены светлые элементы в тёмной теме: модальные окна, формы, таблицы, статусы, селекты, уведомления и карточки.

## Интеграция
- Inference gateway остаётся канонической точкой приёма детекций.
- Camera telemetry API предназначен для реального RTSP/GStreamer worker.
- Capability API сообщает, какие внешние workers подключены.
- Telegram или MAX по-прежнему выбираются при установке.

## Проверки
- Отдельные production-тесты подтверждают пустой старт без fake data.
- Camera CRUD, telemetry, diagnostics, search, guarded delete и model registration покрыты тестами.
- CI собирает API, Web, Telegram и MAX Docker images и выполняет security/dependency audits.
