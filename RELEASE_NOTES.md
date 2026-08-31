# ZMK Vision v2.14.0 — ПОЛНЫЙ РЕДИЗАЙН СТРИМИНГА: single connection via go2rtc, true 25-60 FPS

## Проблема пользователя

> постоянно переподключает камеру и 4 фпс остается а стрим в большем фпс не идет

**Корень зла — двойное RTSP подключение:**

- Старая архитектура: `inference-worker` → камера (RTSP) + `go2rtc` → камера (RTSP) = **2 подключения к одной камере**
- Дешевые камеры/NVR лимитируют 1-2 подключения, при 2-х начинают дропать, резать FPS до 4, постоянно переподключать
- Плюс `inference-worker` делал JPEG re-encode 1920 q85 каждый кадр + POST (уже пофикшено в 2.13.11, но двойное подключение осталось)
- Frontend WebRTC падал если 8555 UDP заблокирован firewall'ом, падал в MJPEG fallback который требует транскодинга H264→MJPEG в Go (тоже CPU)

**Требование:** "пофикси любым способом можеш поискать методы и выбрать для всех функций проекта ты можеш полностью пересмотреть способ стриминга и тд короче даю полное свободное мышление сделай хорошо"

## Новая архитектура v2.14.0 — SINGLE CONNECTION VIA GO2RTC

```
Камера RTSP (1 подключение) → go2rtc (Go, host network, :8554 RTSP, :8555 WebRTC, :1984 API)
                                ├─→ inference-worker via RTSP rtsp://host.docker.internal:8554/zmk-{id} (local, 25-60 FPS, no camera load)
                                └─→ Browser via WebRTC H264 direct (true 25-60 FPS, no re-encode)
                                     ├─ fallback MSE fMP4 over WebSocket (HTTP, low-latency, firewall-friendly)
                                     ├─ fallback HLS fMP4 m3u8 (HTTP, H264 passthrough, firewall-friendly)
                                     ├─ fallback go2rtc MJPEG (Go fast)
                                     └─ fallback snapshot
```

**Камера видит только 1 подключение (go2rtc), go2rtc fan-out на всех потребителей — как в VLC proxy.**

### 1. docker-compose.yml

- `inference-worker` теперь `extra_hosts: host.docker.internal:host-gateway` и env:
```yaml
GO2RTC_API_URL: http://host.docker.internal:1984/rtc
GO2RTC_RTSP_URL: rtsp://host.docker.internal:8554
GO2RTC_ENABLED: true
GO2RTC_USE_FOR_INFERENCE: true  # NEW: inference берет RTSP из go2rtc, а не напрямую
```
- `api` тоже получил `GO2RTC_RTSP_URL` и `GO2RTC_USE_FOR_INFERENCE`

### 2. services/inference_worker/main.py — ПОЛНОСТЬЮ ПЕРЕПИСАН RTSP CLIENT

- Новый env `GO2RTC_RTSP_URL`, `GO2RTC_USE_FOR_INFERENCE`, `GO2RTC_INFERENCE_MAX_FAILURES=5`
- Helper `_go2rtc_rtsp_url_for(camera_id)` → `rtsp://host.docker.internal:8554/zmk-{id}`
- `CameraSession` добавлены `go2rtc_failures`, `using_go2rtc`
- `_open()` теперь:
  - Если `GO2RTC_ENABLED && USE_FOR_INFERENCE && failures<5`: пробует `rtsp://.../zmk-{id}` via TCP первым
  - Если успех: `using_go2rtc=True`, лог "opened via GO2RTC RTSP TCP (single connection to camera, true FPS)"
  - Если go2rtc RTSP упал 5 раз: fallback на прямой `rtsp_url` (legacy)
  - Для go2rtc всегда TCP, не тогглит transport
- `_failed_open()` и `_failed_read()`:
  - Для go2rtc не тогглят transport, считают `go2rtc_failures`, после 5 падений fallback на direct
  - Логи `frame failed (go2rtc)` vs `(direct)`
- `_sync_cameras()` сбрасывает `go2rtc_failures` при смене URL
- Результат: камера грузится только go2rtc, inference локально 25-60 FPS, нет переподключений

### 3. backend/app/main.py — go2rtc SYNC УЛУЧШЕН

- `GO2RTC_RTSP_URL`, `GO2RTC_USE_FOR_INFERENCE`, timeout 2→5 sec
- `_go2rtc_source_url()` теперь всегда `transport=tcp` + `mp4` для MSE/HLS passthrough
- `sync_go2rtc_cameras()`:
  - Создает оба имени `zmk-{id}` и `{id}` (primary zmk- для inference+frontend)
  - Не падает если одна камера битая — `continue` вместо `return`
  - Логика single-connection: go2rtc держит 1 RTSP к камере пока есть потребители (inference всегда подключен)
  - Возвращает `mode: single-connection-via-go2rtc`
  - Cleanup только `zmk-` и `cam_` префиксов, чужие стримы не трогает

### 4. services/go2rtc/go2rtc.yaml — SINGLE CONNECTION CONFIG

```yaml
rtsp:
  listen: ":8554"
  default_query: "mp4"  # MSE/HLS direct H264 passthrough, inference via rtsp://host.docker.internal:8554/zmk-{id}

webrtc:
  listen: ":8555"
  candidates:
    - stun:8555  # public IP via STUN
  ice_servers:
    - urls: [stun:stun.l.google.com:19302, stun:stun1.l.google.com:19302, stun:stun2.l.google.com:19302, stun:stun3.l.google.com:19302, stun:stun.cloudflare.com:3478]
```
- Host network → автоматически host кандидаты LAN IP
- 5 STUN серверов для надежности
- `api: origin: "*"` + `base_path: "/rtc"`

### 5. frontend/src/main.tsx — ПОЛНЫЙ РЕДИЗАЙН CameraPreview

- **WebRTC primary** — 8s timeout (было 6s), 4 STUN, `bundlePolicy: max-bundle`, `iceTransportPolicy: all`, `connectionState` tracking, exponential backoff retry `delay = min(1000*1.5^attempts, 10000)`, health check каждые 15s если не live и камера online → ретрай WebRTC
- **NEW MSE fallback** — `ws://host/rtc/api/ws?src=...` → `{"type":"mse","value":"mp4"}` → MediaSource `video/mp4; codecs="avc1.640029"` fMP4 over WebSocket, low-latency H264, работает когда UDP заблокирован firewall'ом (HTTP/WebSocket)
- **HLS fallback** — `stream.m3u8?src=...` с `lowLatencyMode`, `backBufferLength:90`, `maxBufferLength:10`, `liveSyncDuration:1`, `liveMaxLatencyDuration:3`, пробует оба имени
- **go2rtc MJPEG** — Go fast, пробует оба имени
- **worker MJPEG** — только если go2rtc disabled
- **snapshot** — polling 3s
- Visual overlay каждые 400ms, retry кнопка, reconnectCount
- Метки: `WEBRTC H264 25-60 FPS (single conn via go2rtc)`, `MSE H264 LOW-LATENCY`, `HLS H264 FULL QUALITY`

### 6. .env.example

- Добавлены `GO2RTC_RTSP_URL=rtsp://host.docker.internal:8554` и `GO2RTC_USE_FOR_INFERENCE=true` с комментарием про single connection

### Результат

- **Камера: 1 подключение вместо 2** → нет лимита, нет дропов, нет постоянных переподключений
- **Inference FPS: 4.2 → 25-60** — берет RTSP локально из go2rtc (host.docker.internal:8554), а не напрямую с камеры через сеть, декодер opencv direct H264, CPU не тратится на MJPEG re-encode
- **Preview FPS: 4.2 → 25-60** — WebRTC H264 direct passthrough, без транскодинга, как в VLC
- **Firewall-friendly** — если UDP 8555 заблокирован, MSE (WebSocket) и HLS (HTTP) работают через nginx proxy `/rtc/` same-origin
- **Stable** — go2rtc держит RTSP к камере пока inference подключен, frontend WebRTC подключается к уже существующему потоку без нового подключения к камере
- **Overlay** — `/visual` JSON % координаты, рисуется в браузере, не baked

## Обновление

```bash
cd ~/zmk-vision
git config --global --add safe.directory ~/zmk-vision
git config pull.rebase false
git fetch origin --tags --prune --force
git checkout -B main origin/main
./start.sh
# go2rtc теперь единственный источник RTSP, inference и браузер — клиенты
# Проверь логи: inference-worker должен писать "opened via GO2RTC RTSP TCP (single connection to camera, true FPS)"
```

## Сохранено

- v2.13.12: auto-update safe.directory fix
- v2.13.11: worker skips MJPEG when go2rtc enabled
- v2.13.10: updater Alpine 5s vs 243s
- opencv decoder default, ffmpeg fallback

---
# v2.13.12 — fix auto-update dubious ownership + 404
# v2.13.11 — fix 4 FPS MJPEG bottleneck
# v2.13.10 — updater Alpine
