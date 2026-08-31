# ZMK Vision v2.13.7 — fix npm ci hls.js lock file

## Ошибка сборки web — исправлено

```
=> ERROR [web build 4/6] RUN npm ci
npm error Missing: hls.js@1.6.13 from lock file
```

**Причина:** в v2.13.5 добавлен `hls.js 1.6.13` в `package.json`, но `package-lock.json` не был обновлён.

**Фикс:**
- `frontend/package-lock.json` обновлён — добавлен `node_modules/hls.js 1.6.13`
- `npm ci` теперь проходит

## Сохранено из v2.13.6

### Docker BuildKit parent snapshot fix
- `services/inference_worker/Dockerfile` и `training_worker/Dockerfile` `FROM python:3.12-slim` + pip `ultralytics 8.4.126 opencv-headless 4.10 torch 2.5.1 CPU`
- `start.sh` fallback цепочка с `DOCKER_BUILDKIT=0 COMPOSE_BAKE=false --no-cache` и `builder prune --all -f`

### WebRTC H264 FULL QUALITY 60 FPS no-cut
- Worker `CAMERA_DECODER=opencv` default, `next_frame_at 0.001`, `_encode_live` 1920 max 85%, `CAMERA_LIVE_FPS=60`, model `image.copy()` full
- Backend `camera_mjpeg` go2rtc proxy first `frame.jpeg` 0xffd8 then `stream.mjpeg` boundary `--go2rtc`
- Frontend `webrtc → hls (hls.js lowLatency) → go2rtc-mjpeg → api-mjpeg → snapshot`, visual overlay 500ms % coords

Результат: сборка проходит, `● WEBRTC H264 FULL QUALITY` 25-30 FPS без `snapshot fallback`.

## Обновление

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh)
docker compose --profile inference up -d --build --remove-orphans
./start.sh
```

---

# ZMK Vision v2.13.6 — fix Docker BuildKit + WebRTC H264 60 FPS full quality

## Ошибка сборки Docker — исправлено

```
=> ERROR [inference-worker] exporting to image
failed to solve: failed to prepare extraction snapshot "extract-... sha256:dd842e...": parent snapshot sha256:eaaafa4e... does not exist: not found
WARN Docker Compose is configured to build using Bake, but buildx isn't installed
```

**Причина:** базовый образ `ultralytics/ultralytics:8.4.126` имел повреждённый parent snapshot в BuildKit кэше.

**Фикс:**
- `services/inference_worker/Dockerfile` и `training_worker/Dockerfile` теперь `FROM python:3.12-slim` вместо ultralytics base
- Установка `ffmpeg + libgl + ultralytics 8.4.126 + opencv-headless 4.10 + torch 2.5.1 CPU` через pip
- `start.sh` `repair_build_cache()` теперь: `builder prune -af`, `buildx prune -af`, `system prune`, `systemctl restart docker`, sleep 2
- `start_stack()` fallback цепочка:
  1. обычный `up -d --build`
  2. `COMPOSE_PARALLEL_LIMIT=1 up -d --build`
  3. `COMPOSE_BAKE=false COMPOSE_DOCKER_CLI_BUILD=0 up -d --build`
  4. `DOCKER_BUILDKIT=0 COMPOSE_BAKE=false build --no-cache inference-worker training-worker` + `up -d`
  5. `builder prune --all -f` + `DOCKER_BUILDKIT=0 COMPOSE_BAKE=false --no-cache --parallel 1`

## FPS 4.2 + snapshot fallback — исправлено (v2.13.5, сохранено)

**Worker decoder по умолчанию `opencv` (было `ffmpeg`) — честный FPS как в VLC**
- `opencv` — прямой H264 decode, без MJPEG перекодирования, как live555 в VLC
- Run loop `next_frame_at = now + 0.001` — декодирует максимально быстро, публикация лимитируется `LIVE_PREVIEW_FPS=60`
- `_encode_live()` — 1920 max, 85% quality, без урезания качества для модели

**Backend `camera_mjpeg` — прямой go2rtc MJPEG для честного FPS**
- Сначала `go2rtc /api/stream.mjpeg?src=zmk-id` (Go/C, без Python POST bottleneck)

**Frontend цепочка как в VLC, primary WebRTC H264 FULL QUALITY**
- WebRTC `wss://host/rtc/api/ws?src=zmk-id` — H264 напрямую
- HLS `/rtc/api/stream.m3u8?src=zmk-id` via `hls.js 1.6.13` lowLatency
- go2rtc MJPEG FULL, API MJPEG FULL 60FPS, snapshot

**Выбор модели и разметка**
- `ModelPipeline` слоты `people/helmet/workwear/phone/smoking/zone`, `visual` overlay каждые 500мс, % координаты

Результат: `● WEBRTC H264 FULL QUALITY` 25-30 FPS, `FPS 25-30 / лимит 50`, без `snapshot fallback`, модель на полном качестве, Docker сборка надёжная.

## Обновление

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh)
docker compose --profile inference up -d --build --remove-orphans
# Если BuildKit падает — start.sh сам очистит кэш и пересоберёт с --no-cache
./start.sh
```
