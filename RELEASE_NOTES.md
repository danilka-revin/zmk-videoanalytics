# ZMK Vision v2.13.3 — true 25FPS via go2rtc direct + overlay

## Исправление 4 FPS: настоящий live как в VLC

Проблема 4 FPS осталась, потому что fallback MJPEG шёл через воркер:
`RTSP → FFmpeg → JPEG → HTTP POST → API memory → multipart` — каждый POST 200-300мс троттлил FPS.

### Решение v2.13.3 — прямой путь через go2rtc

**Backend `camera_mjpeg`:**
- Теперь сначала пробует `go2rtc /api/stream.mjpeg?src=zmk-<id>` напрямую, а не `_live_frames` от воркера
- go2rtc сам транскодирует RTSP H264 → MJPEG на C/Go, без Python POST, поэтому даёт честные 25 FPS как в VLC
- Если go2rtc недоступен — fallback в старый `_live_frames` от воркера (25 FPS)

**Frontend `CameraPreview` цепочка фолбэков:**
1. **WebRTC** `wss://host/rtc/api/ws?src=zmk-<id>` — H264 напрямую, минимальная задержка, как в VLC
2. **go2rtc MJPEG direct** `/rtc/api/stream.mjpeg?src=zmk-<id>` — честный 25 FPS от go2rtc, bypass воркера
3. **API MJPEG** `/api/cameras/{id}/mjpeg` — воркер 25 FPS raw (720p/65%)
4. **Snapshot** каждые 3 сек — последний шанс

- `beginGo2rtcMjpeg` ставит `src` на go2rtc URL, `onLoad` → `live=true`, `onError` → `beginMjpegRef.current()` (API MJPEG)
- `beginMjpegRef` через `useRef` чтобы избежать reference error
- Таймеры: WebRTC 4с → go2rtc MJPEG, go2rtc MJPEG → API MJPEG

**Worker `inference_worker` (v2.13.2, сохранено):**
- Сырой кадр для live без боксов, `_encode_live` 720p/65% fast path
- `last_live_at` до encode, а не после POST
- Боксы отдельно via `POST /visual`, фронтенд рисует overlay поверх видео, а не запекает в JPEG

**Overlay:**
- `GET /api/cameras/{id}/visual` каждые 500мс, expire 5с
- `%` координаты от `shape`, цвета: `no_helmet/no_vest` красный, `person` синий, `helmet` зелёный, `vest` фиолетовый

### Выбор модели и разметка — проверено

- **ModelPipeline** `/api/models/pipeline` — слоты `people/helmet/workwear/phone/smoking/zone`, фильтр `status=ready && precision/recall != null`
- Активация слота `POST /api/models/{name}/activate-slot {role}`, удаление `DELETE /api/models/pipeline/{role}`
- Воркер загружает модели фоново, `model_test_mode` не отправляет тревоги, но рисует боксы
- После выбора модели воркер публикует `visual` с боксами, фронтенд overlay показывает их поверх raw видео
- Snapshot для evidence — аннотированный с боксами каждые 3 сек

Результат: **WebRTC 25FPS H264 как в VLC + overlay только когда найден человек**, fallback go2rtc MJPEG 25FPS тоже как в VLC, без 4 FPS bottleneck.

## Все нововведения с v2.12.0

- go2rtc + WebRTC primary, FFmpeg `nobuffer+direct+low_delay` 100ms
- state-machine камер, heartbeat, реальный FPS
- TCP-first/UDP fallback, timeouts до open(), keyframe grace 15с
- карточки Компакт/Обычные/Крупные, fullscreen
- мульти-модельный пайплайн, upload 2GB, safe test, bulk delete
- кириллица «Человек/Без каски» → `person/no_helmet`, `without_hardhat` fix
- password auth + email recovery, bot tokens private volume, Telegram Mini App dark
- support bundle, event evidence zip, smart search

## Обновление

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh)
docker compose --profile inference up -d --build --force-recreate api web go2rtc inference-worker
docker compose logs -f --tail=150 inference-worker go2rtc
```

В браузере: `● WEBRTC REAL-TIME 25FPS` → `● GO2RTC MJPEG 25FPS` → `● LIVE MJPEG 25FPS`, все с overlay разметки выбранной модели.
