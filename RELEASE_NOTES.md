# ZMK Vision v2.2.1 — Worker Reliability Fixes

- Реальная отмена обучения теперь завершает отдельный GPU process, а не только asyncio task.
- Progress callbacks добавлены для capture, pseudo-labeling, training и ONNX export.
- Ошибки дочернего ML process безопасно возвращаются backend.
- Inference worker использует YOLO tracking и передаёт устойчивый person_id для event cooldown.
- Удалённые камеры закрывают RTSP capture и освобождают ресурсы.
- Добавлены OpenCV RTSP open/read timeouts.
- SHA-256 больших моделей считается потоково без загрузки файла целиком в память.
- Telemetry сообщает фактическую скорость обработки worker, а не FPS источника.
- Исправлены cleanup multiprocessing queue/process при error/cancel/shutdown.
