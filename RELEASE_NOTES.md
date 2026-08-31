# ZMK Vision v2.16.3 — fix go2rtc RTSP source options separator (cameras offline)

## Ошибка в логах go2rtc

```
WRN [rtsp] error="streams: Get \"tcp&backchannel=0\": unsupported protocol scheme \"\"" stream=zmk-cam_env_01
```

## Причина

go2rtc парсит опции RTSP-источника как `#key=value`-фрагменты, разделённые символом `#`
(`internal/streams/helpers.go ParseQuery`). Бэкенд собирал источник как
`rtsp://...#transport=tcp&backchannel=0`, то есть через `&`. Из-за этого go2rtc
считал всю строку `tcp&backchannel=0` значением `transport`, уходил в ветку
WebSocket-подключения и падал с `unsupported protocol scheme ""` — камера оставалась офлайн.

## Фикс

**backend/app/main.py `_go2rtc_source_url`:** опции теперь склеиваются через `#`:

```
rtsp://admin:…@…:554/h264/ch01/main/av_stream#transport=tcp#backchannel=0
```

- операторский `transport` (например `udp`) сохраняется, дефолт добавляется только если отсутствует;
- явный `backchannel` оператора не перезаписывается и не дублируется;
- неизвестные опции (например `media=video`) сохраняются;
- `httpx` кодирует `#` в `%23`, поэтому go2rtc получает корректный `src`.

**VERSION / APP_VERSION / package.json:** 2.16.2 → 2.16.3

## Проверка

```bash
cd backend && pytest tests/test_camera_runtime.py -q   # 9 passed
curl "http://HOST:1984/rtc/api/streams" | jq '.zmk-cam_env_01.producers[0].url'
# rtsp://…/av_stream#transport=tcp#backchannel=0
docker logs zmk-vision-go2rtc-1 --tail 20   # больше нет "unsupported protocol scheme"
```

# ZMK Vision v2.16.3 — одна команда снова ведёт на main после merge PR

## Проблема

После merge PR рабочая ветка `arena/…` остаётся на remote (она не удаляется), поэтому
launcher `zmk-vision` продолжал «сидеть» на ней вечно и больше не получал обновления
релизного канала `main`. Одной командой обновить и запустить `main` не получалось.

## Фикс

**installers/bootstrap-linux.sh:** после `git fetch` закреплённой ветки launcher сверяет её
верхний коммит с историей `main` через `git merge-base --is-ancestor`:

- shallow-клоны один раз распаковываются (`git fetch --unshallow origin main`), чтобы
  merge-коммит с его вторым родителем был виден для точной проверки;
- если закреплённый коммит уже в `main` — launcher переключается на `main` и дальше
  `zmk-vision` (одна команда) обновляет и запускает релизный канал, как раньше;
- если ветка ещё не слита — работа на ней продолжается без изменений.

## Проверка

```bash
pytest tests/test_installers.py -q   # в т.ч. test_launcher_detects_merged_branch_and_switches_to_main
```

---

# ZMK Vision v2.14.2 — fix frontend build TS1382 + TS2345

## Ошибка пользователя после обновления на v2.14.1 (из start.sh лога)

```
#6 11.43 src/main.tsx(380,159): error TS1382: Unexpected token. Did you mean `{'>'}` or `&gt;`?
#6 11.43   if(!src)return <div className="feed-empty">{telemetryStale?<><WifiOff size={20}/><span>Нет телеметрии</span><small>worker давно не подтверждал поток (go2rtc->inference RTSP single conn)</small></>:status==='connecting'?...
#6 ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 2
```

**Причины:**

1. **TS1382 из-за `->` в JSX тексте** — в `main.tsx:380` строка `<small>worker давно не подтверждал поток (go2rtc->inference RTSP single conn)</small>` содержит `->` прямо в JSX тексте. TypeScript JSX парсер видит `>` как неожиданный токен (думает что это закрытие тега) и требует `{'>'}` или `&gt;`. Аналогично вторая строка `(go2rtc->inference RTSP)` тоже ломала сборку.

2. **Codec строка `addSourceBuffer('video/mp4; codecs="avc1.640029"')`** — двойные кавычки внутри одинарных в TSX минифицированной строке тоже могли триггерить парсер (в логе было на 380 строке из-за склейки, но реально проблема еще и в 158 строке). Плюс `Uint8Array` → `BufferSource` несовместимость в TS5 (SharedArrayBuffer).

## Фикс v2.14.2

**frontend/src/main.tsx:**
- `go2rtc->inference` → `go2rtc → inference` (unicode стрелка) в JSX тексте — больше не парсится как `>` токен
- `addSourceBuffer('video/mp4; codecs="avc1.640029"')` → `addSourceBuffer("video/mp4; codecs=\"avc1.640029\"")` — экранированные двойные кавычки внутри двойных
- `sourceBuffer.appendBuffer(Uint8Array)` → `sourceBuffer.appendBuffer(buf as unknown as BufferSource)` — фикс TS2345 `Uint8Array<ArrayBufferLike> not assignable to BufferSource`
- `queue.shift()!` → `queue.shift()! as unknown as BufferSource` аналогично
- Все MSE пути теперь с кастом, сборка `tsc -b && vite build` проходит

**VERSION / APP_VERSION / package.json:** 2.14.1 → 2.14.2

**Сохранено из v2.14.1:**
- Неубиваемый updater: `git reset --hard HEAD` + `git clean -fd` перед fetch/checkout, fallback fetch, checkout FETCH_HEAD → origin/main → tag
- Single connection via go2rtc: камера → go2rtc (1 conn) → inference RTSP `rtsp://host.docker.internal:8554/zmk-{id}` 25-60 FPS + browser WebRTC H264 direct 25-60 FPS
- Frontend WebRTC primary 8s timeout, 4 STUN, exponential backoff, MSE fMP4 over WS, HLS lowLatency

## Ручное обновление если застряли на 2.14.1 с ошибкой сборки

```bash
cd ~/zmk-vision
git reset --hard HEAD
git clean -fd
git fetch origin --tags --prune --force
git checkout -B main origin/main
./start.sh
# теперь 2.14.2, сборка проходит
```

## Проверка

```bash
cd ~/zmk-vision
cat VERSION # 2.14.2
docker compose build web --no-cache 2>&1 | tail -20
# vite v8.2.1 building... ✓ built in ...ms — без TS1382
docker logs zmk-vision-inference-worker-1 --tail 5
# opened via GO2RTC RTSP TCP (single connection to camera, true FPS)
```

---
# v2.14.1 — fix updater: local changes + untracked Dockerfile.slim blocking checkout
- `installers/auto-update.sh` и `bootstrap-linux.sh`: `reset --hard` + `clean -fd` перед fetch/checkout, фикс `origin/v2.14.0 is not a commit`
- `start.sh`: repair_build_cache теперь `pull.ff only`
- Ручной фикс: `git reset --hard && clean -fd && checkout -B main origin/main`

# v2.14.0 — ПОЛНЫЙ РЕДИЗАЙН СТРИМИНГА: single connection via go2rtc, true 25-60 FPS
- go2rtc единственный RTSP клиент к камере, inference и браузер — клиенты go2rtc
- Inference: 4.2 → 25-60 FPS локально rtsp://host.docker.internal:8554/zmk-{id}
- Preview: 4.2 → 25-60 FPS WebRTC H264 passthrough как в VLC
- Frontend: WebRTC 8s + MSE + HLS fallback, exponential backoff, 15s health check

# v2.13.12 — fix auto-update dubious ownership + 404
# v2.13.11 — fix 4 FPS MJPEG bottleneck
# v2.13.10 — updater Alpine 5s vs 243s
