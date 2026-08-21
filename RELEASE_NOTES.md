# ZMK Vision v2.2.0 — Full RTSP Inference & Advanced Training

## Реальный inference worker
- Подключение к сохранённым RTSP URL через OpenCV.
- Горячая загрузка активного ONNX/TensorRT/PyTorch артефакта.
- Проверка SHA-256 перед загрузкой.
- NVIDIA/CPU inference с настраиваемым confidence.
- Отправка фактической camera telemetry каждые 10 секунд.
- Передача детекций с timestamp, bbox и idempotency ID в gateway.
- Поддержка классов нарушений: no_helmet, no_vest, phone_usage, smoking, restricted_zone, immobility.
- Выделенный защищённый internal API с отдельным worker token.

## Расширенное автодообучение
Настройки из Web/API передаются GPU worker и сохраняются в задаче:
- image_count;
- epochs;
- batch;
- image size;
- patience;
- pseudo-label confidence;
- validation split;
- capture FPS.

Worker создаёт раздельные train/val наборы, выполняет YOLO11n fine-tuning, экспорт ONNX, callback прогресса, регистрацию модели и реальную cancellation.

## Интеграции
- Webhook СКУД теперь фактически отправляется для принятых событий.
- Ошибки доставки записываются в системный журнал.
- Installers предлагают отдельно включить NVIDIA training и RTSP inference workers.
