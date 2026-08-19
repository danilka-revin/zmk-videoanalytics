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
    "detection_id": "cam_01-frame_938-person_2-no_helmet",
    "bbox": [120, 80, 430, 710]
  }]
}
```

Gateway для каждой детекции проверяет:

1. Pydantic-схему и допустимый тип события.
2. Совпадение `model_name` с атомарно активированной моделью. Старые workers после hot-swap получают `stale_model`.
3. Существование камеры, online-состояние и флаг AI.
4. Индивидуальный порог confidence для каждого типа события.
5. Корректность bbox и допустимое окно timestamp: не старше 7 дней и не более чем на 10 минут в будущем.
6. Идемпотентность по `detection_id`: повторная доставка возвращает существующий `event_id`, не создавая дубль.
7. Классификацию severity и запись события в БД.
8. Audit log всего batch с количеством принятых и отклонённых детекций.

Чтение активной модели и запись batch выполняются в одной транзакции, поэтому hot-swap не может разорвать обрабатываемый пакет. Ответ содержит `event_id` для принятых событий и причину для каждого отклонения. Событие сразу доступно в `/api/events`, отчётах и панели.

## Production transport

HTTP-контракт является каноническим. При масштабировании тот же JSON публикуется worker-ом в Redis Streams, consumer вызывает тот же validation/service слой. Для гарантированной доставки используются consumer groups, idempotency key и dead-letter stream; это следующий production-этап после подключения реальных RTSP и GPU worker.
