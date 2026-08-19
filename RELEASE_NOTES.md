# ZMK Vision v1.2.4 — Model Pipeline Hardening

## Модель и inference gateway
- Добавлена идемпотентность детекций по `detection_id`; повторная доставка возвращает существующий `event_id`.
- Добавлена строгая проверка bbox и временного окна timestamp.
- Для всех шести типов событий используются отдельные настраиваемые confidence thresholds.
- Обработка batch и чтение активной модели выполняются в одной транзакции с hot-swap.
- RTSP/model payload проходит расширенную валидацию.

## Реестр и hot-swap
- Повторная активация текущей модели стала идемпотентной и не создаёт ложный audit log.
- Добавлен quality gate по минимальным Precision/Recall.
- Добавлен health endpoint активной модели и автоматическое восстановление некорректной ссылки active_model.
- Время control-plane переключения измеряется отдельно.

## Обучение
- Одновременно допускается одна GPU-задача, что предотвращает конкуренцию за VRAM.
- Добавлена отмена через API, Web и Telegram.
- Background tasks отслеживаются и корректно завершаются при остановке API.
- Ошибки, отмена и перезапуск получают явные terminal statuses.
- Коллизии имён обучаемых моделей блокируются.

## Проверки
Расширенный набор regression-тестов проверяет модельный gateway, idempotency, thresholds, geometry, timestamps, hot-swap, health, quality gate, single-job guard и cancellation.
