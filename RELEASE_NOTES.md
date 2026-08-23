# ZMK Vision v2.12.0 — новый надёжный контур RTSP-камер

## Главное

Камеры и AI-модели теперь независимы. `inference-worker` сначала запускает RTSP-превью, телеметрию и reconnect-машину, а Ultralytics/PyTorch загружаются лениво и в фоне только при наличии активной модели. Ошибка CUDA, отсутствующая модель или повреждённый артефакт больше не должны останавливать просмотр камеры.

## Камеры

- новая машина состояний: `connecting → online / recovering / offline`;
- persistent FFmpeg decoder по умолчанию: `-fflags +discardcorrupt`, `-err_detect ignore_err`, error concealment и MJPEG pipe, чтобы повреждённые RTP/H.264 кадры не давали чёрное превью и не роняли worker;
- отдельная сессия и расписание на каждую камеру: сбой одной камеры не блокирует остальные;
- native OpenCV/FFmpeg чтение больше не отменяется из Python посередине кадра, что исключает crash worker с `exit code 139` при повреждённом потоке;
- TCP-first / UDP fallback в `RTSP_TRANSPORT=auto`;
- таймауты открытия и чтения RTSP (`RTSP_OPEN_TIMEOUT_MS`, `RTSP_READ_TIMEOUT_MS`) передаются OpenCV до `open()`, а не после него; это убирает встроенное ожидание около 30 секунд;
- после RTSP-подключения worker ждёт H.264 keyframe (`RTSP_KEYFRAME_GRACE_SECONDS=15`) и не сбрасывает поток на первых повреждённых delta-кадрах;
- используется актуальный FFmpeg socket option `timeout` в микросекундах (`RTSP_TIMEOUT_OPTION=timeout`), с опциональным legacy-режимом `stimeout`;
- безопасная публикация превью без AI-модели;
- реальный FPS по окну телеметрии, без стартовых всплесков;
- worker heartbeat в API и понятные диагностические данные;
- реальный MJPEG preview endpoint в панели: кадры отображаются с `CAMERA_LIVE_FPS` (до `fps_limit` камеры), а не редким snapshot polling;
- low-latency FFmpeg/MJPEG pipeline: `nobuffer`, `avioflags=direct`, RTP max delay 100 мс, immediate packet flush и Nginx без proxy buffering;
- training-worker всегда запускается с базовым стеком; GPU включается автоматически при NVIDIA Container Toolkit, иначе доступен CPU fallback;
- хранение безопасной последней причины ошибки без RTSP-логина и пароля;
- кнопка «Перезапустить» в карточке камеры: worker получает новый token конфигурации и заново открывает поток;
- `RTSP_CAM_01` автоматически создаёт первую камеру на пустой базе данных.

## Эксплуатация

Для чистой установки или обновления Git-копии одной командой появился `installers/bootstrap-linux.sh`: он клонирует/fast-forward обновляет проект, скрыто спрашивает RTSP URL только при первом запуске и вызывает штатный установщик. Повторный запуск сохраняет `.env`, `data` и Docker volumes.

После обновления:

```bash
docker compose --profile inference up -d --build --force-recreate api inference-worker
docker compose --profile inference logs -f --tail=150 inference-worker
```

Нормальный запуск начинается так:

```text
inference: camera runtime started (...)
inference: no active model; camera preview and telemetry remain enabled
inference: camera cam_... opened via TCP
```

Не публикуйте RTSP URL или пароль в логах и обращениях в поддержку.

## Проверки

- backend API tests;
- worker camera state-machine tests без GPU/OpenCV runtime;
- TypeScript lint/build;
- Ruff, Bandit, pip-audit, npm audit;
- Docker Compose/image build в GitHub Actions.
