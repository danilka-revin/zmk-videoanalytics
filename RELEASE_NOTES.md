# ZMK Vision v2.13.2 — true 25FPS VLC-like с лёгким оверлеем

## Главное: 4 FPS → 25 FPS как в VLC

Предыдущая сборка всё ещё показывала 4 кадра из-за того, что каждый кадр перерисовывался заново с нуля с запечёнными боксами:

- `_publish_live` обновлял `last_live_at` **после** `POST`, поэтому задержка сети (200-300мс) троттлила FPS до 4;
- `_encode_snapshot` ресайз до 960 и 3 попытки качества (75/60/45) — тяжело для 25 FPS;
- `frame()` публиковал **аннотированный** кадр (copy + draw) для live, а не сырой.

Исправлено в v2.13.2:

- **Worker теперь публикует сырой кадр для live** — без копирования и без боксов, `720p max / 65% JPEG` в `_encode_live` (быстрый путь);
- **`last_live_at` обновляется до encode**, а не после POST — POST не троттлит FPS;
- **Боксы отправляются отдельно** через `POST /api/internal/cameras/{id}/visual` (лёгкий JSON), фронтенд рисует их как HTML overlay поверх видео, а не запекает в JPEG;
- **Backend** хранит `_live_visuals` dict + endpoints:
  - `POST /api/internal/cameras/{id}/visual` (worker token) — shape + boxes
  - `GET /api/cameras/{id}/visual` — для браузера, expire 5 сек, age_seconds
- **Frontend** `CameraPreview`:
  - `boxes/shape` state, `fetchVisual()` каждые 500мс
  - `renderOverlay()` — абсолютные div'ы в % от shape, цвета: `no_helmet/no_vest` красный `#ff3b30`, `person` синий `#007aff`, `helmet` зелёный, `vest` фиолетовый
  - Обёртка `camera-feed-wrap` с `position:relative`, overlay `pointer-events:none`, z-index 4
  - WebRTC `● WEBRTC REAL-TIME 25FPS`, MJPEG `● LIVE MJPEG 25FPS` — оба с оверлеем
- **CSS** `.camera-feed-wrap`, `.camera-overlay`, `.camera-box` добавлены

Результат: **сырой поток идёт на полной скорости как в VLC (25 FPS)**, разметка накладывается сверху только если найден человек/нарушение, без перерисовки каждого кадра.

### Предыдущие исправления v2.13.0/v2.13.1 (сохранены)

- WebRTC теперь запрашивает `zmk-<id>` (совпадает с API), dual STUN, bundlePolicy, retry 1с, таймаут 5с;
- API создаёт оба имени `zmk-<id>` и `<id>`, чистит `cam_*`/`zmk-*`;
- `CAMERA_LIVE_FPS` default `25` (было `0`), `0` = отключить MJPEG;
- `go2rtc.yaml` low-latency: TCP, `nobuffer`, `avioflags=direct`, `max_delay 100ms`, STUN кандидаты, host-network 1984/8555;
- `nginx` без буферизации для `/rtc/` и MJPEG, CSP `blob:`.

## Все нововведения с v2.12.0

### Камеры и стриминг
- go2rtc + WebRTC primary: H.264 напрямую, без перекодирования каждого кадра в MJPEG;
- FFmpeg decoder `nobuffer+genpts+discardcorrupt`, `avioflags=direct`, `low_delay`, jitter 100ms;
- state-machine `connecting → online / recovering / offline`, heartbeat, реальный FPS;
- TCP-first / UDP fallback `RTSP_TRANSPORT=auto`, timeouts до `open()`, keyframe grace 15с;
- карточки `Компакт / Обычные / Крупные`, fullscreen, FPS `факт / лимит` до 60;
- кнопка «Перезапустить», `RTSP_CAM_01` bootstrap.

### Модели и AI
- мульти-модельный пайплайн `people/helmet/workwear/phone/smoking/zone`;
- upload локальных артефактов до 2GB через nginx без буферизации;
- safe test lifecycle, bulk delete, self-heal one-command;
- нормализация кириллицы «Человек/Без каски/Жилет» → `person/no_helmet/vest`, токены `without_hardhat`;
- `CAMERA_INFERENCE_FPS=8` отдельно от превью.

### Безопасность и боты
- password auth `ZMK_PASSWORD_AUTH`, сессии 12ч, email recovery SMTP;
- bot tokens в private volume `.bot-tokens`, Telegram Mini App dark, @username, tokenless MAX;
- support bundle, event evidence zip с кадрами, smart search с опечатками.

### Эксплуатация одной командой
- `bootstrap-linux.sh` — клон/fast-forward, миграция legacy `ZMK_REF=main`, `.zmk-ref` трекинг, `zmk-vision`;
- `frontend/Dockerfile` — `GO2RTC_UPSTREAM` via `envsubst`, `RUN nginx -t` отдельным слоем;
- `docker-compose.yml` — go2rtc host network 1984/8555, training-worker always-on CPU/GPU.

### Проверки
- backend 113, integration 52 passed 1 skipped, ruff, bandit, pip-audit, npm build.

## Обновление

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh)
docker compose --profile inference up -d --build --force-recreate api web go2rtc inference-worker
docker compose logs -f --tail=150 inference-worker
```

Лог должен показать `received first decoded frame` и live 25 FPS без baked overlay, боксы приходят отдельно и рисуются поверх.
