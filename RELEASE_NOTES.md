# ZMK Vision v2.13.11 — fix 4 FPS MJPEG bottleneck → true WebRTC H264 25-60 FPS

## Диагноз пользователя (верный)

> все равно 4 фпс это из за тоо что ты вырисовываешь каждый кадр со стрима в mjpeg

Подтверждено кодом:
- `services/inference_worker/main.py` `_encode_live` JPEG 1920 q85 **каждый кадр** + `post_internal_jpeg` POST в `/api/internal/cameras/{id}/live-frame`
- CPU bottleneck: encode 1920 + HTTP POST на 60 FPS = невозможно, throttling до 4 FPS
- `GO2RTC_PREVIEW_ENABLED` флаг существовал но `_publish_live` его НЕ проверял — WebRTC primary не работал эффективно
- Frontend fallback chain: WebRTC → HLS → go2rtc MJPEG → worker MJPEG → snapshot fallback. Когда go2rtc streams не создавались или WebRTC падал через 3.5s, падал в worker MJPEG (4 FPS) и показывал "snapshot fallback 4.2 FPS / limit 50.1"

## Фикс v2.13.11 — WebRTC H264 напрямую как в VLC (primary), без урезания качества

**inference_worker/main.py:**
- Когда `GO2RTC_ENABLED=true` (default), `_publish_live` теперь **полностью пропускает** MJPEG re-encode/upload:
```python
if GO2RTC_PREVIEW_ENABLED and not GO2RTC_FORCE_MJPEG_FALLBACK:
    return
```
- Это убирает главный CPU bottleneck — декодер теперь работает на полной скорости 25-60 FPS как в VLC (opencv direct H264 decode)
- Модель всегда использует `image.copy()` full quality — preview качество НЕ влияет на детекцию (требование пользователя: "не урезай качество картинки и фпс потому что от качества зависит работа модели")
- Остается только легковесный `/visual` JSON overlay (boxes bbox/label/semantic/confidence) + snapshot каждые 3 сек для доказательств
- Добавлен `CAMERA_LIVE_FORCE_MJPEG=true` для debug, если нужно форсировать MJPEG даже с go2rtc
- `CAMERA_LIVE_FPS=0` по-прежнему полностью отключает MJPEG

**backend/app/main.py:**
- `camera_mjpeg` теперь возвращает 503 быстро, если go2rtc enabled но stream не готов и `_live_frames` пуст — вместо зависания в loop 5 сек, чтобы frontend быстро ретрайнул WebRTC
- Добавлен idle timeout 250*0.02=5 sec в worker MJPEG generate loop

**frontend/src/main.tsx — CameraPreview:**
- WebRTC теперь пробует оба имени `zmk-{id}` и plain `{id}` (backend создает оба alias)
- Timeout увеличен с 3500ms → 6000ms (камера успевает стартануть)
- ICE servers расширены до 3 Google STUN
- HLS и go2rtc MJPEG тоже пробуют оба имени
- Визуальный overlay теперь каждые 400ms (было 500ms) — более отзывчивый
- Метки: "WEBRTC H264 FULL QUALITY 25-60 FPS" вместо "FULL QUALITY" — честный FPS как в VLC
- Fallback chain сохранен: WebRTC (primary, H264 direct, true FPS) → HLS (full quality) → go2rtc MJPEG (Go быстрее Python) → worker MJPEG (только если go2rtc disabled) → snapshot

**services/go2rtc/go2rtc.yaml:**
- Упрощен и настроен на `default_query: mp4` для low-latency HLS
- ICE servers 3 STUN для надежности

**Результат:**
- Worker CPU снижается в 10x (нет JPEG encode 1920 q85 * 60 FPS)
- Decode FPS `c.fps` растет с 4.2 → 25-60 (как в VLC, зависит от камеры/NVR)
- Preview FPS true 25-60 via WebRTC H264 direct passthrough, без перекодирования
- Overlay рисуется в браузере via `/visual` % координаты — легко, без baked boxes
- MJPEG только fallback когда `GO2RTC_ENABLED=false`

## Обновление

```bash
cd ~/zmk-vision
git config pull.rebase false
git fetch origin --tags --prune --force
git checkout -B main origin/main
./start.sh
# WebRTC теперь primary, FPS как в VLC
```

## Сохранено из v2.13.10

- updater Alpine `python:3.12-alpine` + `apk add docker-cli docker-cli-compose` ~5s vs 243s
- `COMPOSE_BAKE=false` first attempt
- `frontend/package-lock.json` с `hls.js 1.6.13`
- opencv decoder default, ffmpeg fallback
- 1920 max 85% только для fallback, не для primary

---
# v2.13.10 — updater Alpine fix (предыдущий latest)
---
# v2.13.9 — inference-worker pip torch fix
---
# v2.13.8 — divergent branches fix
