# ZMK Vision v2.13.4 — WebRTC H264 напрямую, без урезания качества и FPS

## Главное: модель работает на полном кадре, live без урезания

Пользователь: "не урезай качество картинки и фпс потому что от качества зависит работа модели"

Исправлено:

- **WebRTC H264 напрямую как в VLC — primary**, без транскодирования, полное качество и полный FPS камеры
  - go2rtc проксирует RTSP H264 → WebRTC без перекодирования, `transport=tcp`, `nobuffer`, `low_delay`
  - Фронтенд `CameraPreview` запрашивает `zmk-<id>`, dual STUN, `bundlePolicy: max-bundle`, retry 1с, таймаут 4с → go2rtc MJPEG
  - Видео элемент `object-fit: cover`, `autoPlay muted playsInline`, без ресайза

- **MJPEG fallback теперь тоже без урезания: 60 FPS, 85% качество, 1920 max**
  - Было: 720p/65% — урезало качество
  - Стало: `_encode_live` 1920 max, 85% quality, один encode, без цикла 75/60/45
  - `.env.example` и `docker-compose.yml` `CAMERA_LIVE_FPS=60` (было 25)
  - Модель всегда использует полный кадр `image.copy()` из оригинального декодера, live JPEG не влияет на детекцию

- **Модель и разметка — проверено, работает**
  - `ModelPipeline` `/api/models/pipeline` — слоты `people/helmet/workwear/phone/smoking/zone`
  - Активация `POST /api/models/{name}/activate-slot {role}`, воркер грузит фоново
  - Воркер публикует боксы отдельно `POST /internal/.../visual` → фронтенд overlay каждые 500мс, expire 5с
  - Overlay — HTML div'ы в % от `shape`, цвета: `no_helmet/no_vest` красный, `person` синий, `helmet` зелёный
  - Snapshot для evidence — аннотированный каждые 3 сек

- **Цепочка фолбэков без потери качества:**
  1. WebRTC H264 FULL QUALITY (primary, как VLC)
  2. go2rtc MJPEG FULL `/rtc/api/stream.mjpeg?src=zmk-id` — 25-30 FPS от go2rtc, bypass воркера
  3. API MJPEG FULL 60FPS — воркер raw 1920/85%
  4. Snapshot

- **Backend `camera_mjpeg`**: сначала пробует go2rtc direct MJPEG для честного FPS, fallback в `_live_frames`

Результат: **WebRTC H264 напрямую как в VLC, без урезания качества и FPS, модель работает на полном кадре**, overlay лёгкий, 4 FPS bottleneck убран.

## Все нововведения с v2.12.0

- go2rtc + WebRTC primary, FFmpeg `nobuffer+direct+low_delay` 100ms
- state-machine камер, heartbeat, реальный FPS
- TCP-first/UDP fallback, timeouts, keyframe grace 15с
- карточки Компакт/Обычные/Крупные, fullscreen
- мульти-модельный пайплайн, upload 2GB, safe test
- кириллица «Человек/Без каски» → `person/no_helmet`, `without_hardhat` fix
- password auth, bot tokens private volume, Telegram Mini App dark
- support bundle, event evidence zip, smart search

## Обновление

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh)
docker compose --profile inference up -d --build --force-recreate api web go2rtc inference-worker
```

В браузере: `● WEBRTC H264 FULL QUALITY` → `● GO2RTC MJPEG FULL` → `● LIVE MJPEG FULL 60FPS`, overlay выбранной модели, модель на полном качестве.
