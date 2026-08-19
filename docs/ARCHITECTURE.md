# Архитектура

```mermaid
flowchart LR
  C[10 × iFlow RTSP] --> I[Ingestion / GStreamer]
  I --> R[(Redis Streams)] --> AI[YOLO / TensorRT worker]
  AI --> E[Event API] --> DB[(PostgreSQL / TimescaleDB)]
  E --> UI[React Console]
  E --> T[Telegram Bot]
  E --> M[(MinIO Archive)]
  E --> S[СКУД Webhook]
```

Текущий MVP объединяет control-plane в одном FastAPI-процессе и использует SQLite, чтобы запускаться одной командой. Границы API соответствуют будущим сервисам. В production inference отделяется в GPU worker, кадры передаются через Redis Streams, метаданные — PostgreSQL, архив — MinIO.

## Целевые показатели эксплуатации
- 10 одновременных потоков по 5–10 FPS.
- Latency события < 2 секунд.
- Precision ≥ 90%, Recall ≥ 85% — проверяются на размеченном наборе площадки, а не гарантируются кодом.
- Работа без внешнего интернета; секреты и видеоданные остаются on-premise.
