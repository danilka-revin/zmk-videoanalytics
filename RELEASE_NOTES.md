# ZMK Vision v2.12.0 — новый надёжный контур RTSP-камер

## Главное

Камеры и AI-модели теперь независимы. `inference-worker` сначала запускает RTSP-превью, телеметрию и reconnect-машину, а Ultralytics/PyTorch загружаются лениво и в фоне только при наличии активной модели. Ошибка CUDA, отсутствующая модель или повреждённый артефакт больше не должны останавливать просмотр камеры.

## Камеры

- новая машина состояний: `connecting → online / recovering / offline`;
- отдельная сессия и расписание на каждую камеру: сбой одной камеры не блокирует остальные;
- TCP-first / UDP fallback в `RTSP_TRANSPORT=auto`;
- таймауты открытия и чтения RTSP (`RTSP_OPEN_TIMEOUT_MS`, `RTSP_READ_TIMEOUT_MS`);
- безопасная публикация превью без AI-модели;
- реальный FPS по окну телеметрии, без стартовых всплесков;
- worker heartbeat в API и понятные диагностические данные;
- хранение безопасной последней причины ошибки без RTSP-логина и пароля;
- кнопка «Перезапустить» в карточке камеры: worker получает новый token конфигурации и заново открывает поток;
- `RTSP_CAM_01` автоматически создаёт первую камеру на пустой базе данных.

## Эксплуатация

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
