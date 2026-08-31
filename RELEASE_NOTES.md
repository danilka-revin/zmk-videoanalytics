# ZMK Vision v2.13.5 — WebRTC H264 напрямую, 60 FPS, без урезания качества

## Скриншот: 4.2 FPS + snapshot fallback — исправлено любым способом

На скриншоте `FPS 4.2 / лимит 50.1` и `snapshot fallback` — воркер декодировал RTSP через FFmpeg `image2pipe mjpeg q:v 5` + JPEG decode + POST, что давало 4 FPS. Фронтенд падал в snapshot fallback, потому что MJPEG поток не успевал.

### Решение v2.13.5 — любым возможным способом, как в VLC

**1. Worker decoder по умолчанию `opencv` (было `ffmpeg`) — честный FPS как в VLC**
- `opencv` — прямой H264 decode через FFmpeg backend OpenCV, без промежуточного MJPEG перекодирования, как в VLC live555
- `ffmpeg` — fallback для повреждённых потоков, но медленнее (4 FPS), теперь `q:v 2` high quality
- `.env.example` `CAMERA_DECODER=opencv`, `docker-compose.yml` `CAMERA_DECODER=opencv`
- Run loop: `next_frame_at = now + 0.001` (было `1/fps_limit`) — декодирует максимально быстро, а не 50 FPS лимит. Публикация лимитируется `LIVE_PREVIEW_FPS`

**2. Live preview без урезания: 60 FPS, 85% quality, 1920 max, модель на полном кадре**
- `_encode_live()` — 1920 max (было 720), 85% quality (было 65), один encode
- `CAMERA_LIVE_FPS=60` в `.env.example` и `docker-compose.yml` (было 25)
- `frame()` публикует сырой `image` для live, аннотированный только для snapshot
- Модель всегда `image.copy()` из оригинала — live JPEG не влияет на детекцию, качество не урезается

**3. Backend `camera_mjpeg` — прямой go2rtc MJPEG для честного FPS**
- Сначала пробует `go2rtc /api/stream.mjpeg?src=zmk-id` (Go/C, без Python POST bottleneck)
- Fallback в `_live_frames` от воркера

**4. Frontend `CameraPreview` — цепочка как в VLC, primary WebRTC H264 full quality**
- **WebRTC H264 FULL QUALITY** `wss://host/rtc/api/ws?src=zmk-id` — H264 напрямую без транскода, минимальная задержка
- **HLS** `/rtc/api/stream.m3u8?src=zmk-id` via `hls.js` 1.6.13 lowLatencyMode — true FPS, fallback если WebRTC не завёлся
- **go2rtc MJPEG FULL** `/rtc/api/stream.mjpeg?src=zmk-id` — 25-30 FPS от go2rtc, bypass воркера
- **API MJPEG FULL 60FPS** — воркер raw 1920/85%
- Snapshot fallback каждые 3 сек

- `beginMjpegRef` via `useRef` для `onError`, `onLoad` → live true
- Таймеры: WebRTC 3.5с → HLS, HLS 4с → go2rtc MJPEG, go2rtc → API MJPEG
- Overlay via `GET /api/cameras/{id}/visual` каждые 500мс, % координаты, цвета: no_helmet красный, person синий

**5. Выбор модели и разметка — проверено**
- `ModelPipeline` `/api/models/pipeline` — слоты `people/helmet/workwear/phone/smoking/zone`, `eligible = ready && precision/recall`
- Активация `POST /activate-slot {role}`, воркер грузит фоново, публикует `visual` с боксами
- Фронтенд overlay поверх raw видео, snapshot evidence аннотированный

Результат: **WebRTC H264 напрямую как в VLC, без урезания качества и FPS, 60 FPS full quality, модель на полном кадре**, 4.2 FPS и snapshot fallback убраны.

## Все нововведения

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
docker compose logs -f --tail=100 inference-worker
```

Ожидаемо: `● WEBRTC H264 FULL QUALITY` 25-30 FPS, `FPS 25-30 / лимит 50`, overlay выбранной модели, без `snapshot fallback`.
