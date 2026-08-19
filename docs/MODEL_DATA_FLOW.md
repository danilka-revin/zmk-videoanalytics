# Передача данных от модели к системе

## Контракт

Inference worker отправляет пакет в `POST /api/inference/detections`:

```json
{
  "detections": [{
    "camera_id": "cam_01",
    "model_name": "siz-guard-v2.1",
    "timestamp": "2026-08-19T14:30:00+07:00",
    "event_type": "no_helmet",
    "confidence": 0.94,
    "person_id": "P-1024",
    "bbox": [120, 80, 430, 710]
  }]
}
```

Gateway для каждой детекции проверяет:

1. Pydantic-схему и допустимый тип события.
2. Совпадение `model_name` с атомарно активированной моделью. Старые workers после hot-swap получают `stale_model`.
3. Существование камеры, online-состояние и флаг AI.
4. Порог confidence из настроек админ-панели.
5. Классификацию severity и запись события в БД.
6. Audit log всего batch с количеством принятых и отклонённых детекций.

Ответ содержит `event_id` для принятых событий и причину для каждого отклонения. Событие сразу доступно в `/api/events`, отчётах и панели.

## Production transport

HTTP-контракт является каноническим. При масштабировании тот же JSON публикуется worker-ом в Redis Streams, consumer вызывает тот же validation/service слой. Для гарантированной доставки используются consumer groups, idempotency key и dead-letter stream; это следующий production-этап после подключения реальных RTSP и GPU worker.
