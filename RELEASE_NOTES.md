# ZMK Vision v2.17.0 — вкладка «Логи»: единый журнал всего проекта

## Зачем

Раньше технический журнал был разорван: структурированные записи API лежали в
SQLite (и были видны только в Админ → Логи в виде отчёта об ошибках), а вывод
inference worker, training worker, ботов и updater — только в
`docker compose logs <сервис>` на сервере. Чтобы разобрать баг, нужно было идти
в терминал и собирать картину вручную из пяти мест.

Теперь в боковом меню есть вкладка **«Логи»** с единым потоком всего проекта.

## Что показывает вкладка

- **Все компоненты в одном потоке:** подсистемы API (камеры, события, модели,
  доступ, интеграции, боты), строки самого процесса API (uvicorn,
  необработанные исключения, неуспешные запросы `4xx/5xx`) и зеркало вывода
  отдельных процессов: inference worker, training worker, Telegram/MAX боты,
  updater.
- **Фильтр по уровню:** чипы `Все / Ошибки / CRITICAL / ERROR / WARNING / INFO /
  DEBUG` со счётчиками за период; уровень можно задать списком
  (`level=ERROR,CRITICAL`).
- **Фильтр по компоненту:** чипы с количеством записей и ошибок; компоненты без
  записей показаны отдельно («молчит») — молчание worker-а само по себе симптом.
- **Поиск по тексту** (регистронезависимый, включая кириллицу), период
  `1 ч / 6 ч / 24 ч / 7 дней / 30 дней`, лимит строк.
- **Live-режим:** автообновление каждые 3 секунды — удобно смотреть, что
  происходит прямо во время воспроизведения бага.
- **Раскрытие строки:** полный текст (включая многострочные traceback),
  компонент, источник, камера, копирование одной строки или всего среза.
- **Выгрузка CSV** текущего среза — чтобы приложить журнал к баг-репорту.
- **Бейдж в меню:** количество ERROR/CRITICAL за 24 часа (`log_errors_24h` в
  `/api/dashboard`).

## Как это работает

- `GET /api/logs/project?service=&level=&q=&camera_id=&hours=&limit=` — единый
  срез: строки из таблицы `logs` + кольцевой буфер процесса API (2000 строк,
  `PROJECT_LOG_RUNTIME_LIMIT`). Ответ содержит `counts` по уровням, `sources`
  по компонентам и `items`.
- `GET /api/logs/project.csv` — тот же срез в CSV.
- `POST /api/service-logs` — приём зеркала журнала от отдельных процессов
  (inference worker шлёт worker-токеном, training worker и боты — сервисным
  токеном). Пакет до 200 строк, квота `PROJECT_LOG_SHIP_RATE` строк в минуту на
  компонент, уровни и метки времени нормализуются, `camera_id` проверяется.
- **inference worker:** tee на `stdout`/`stderr` + отправка раз в 5 секунд из
  управляющего цикла; при недоступном API строки возвращаются в буфер.
- **training worker:** этапы задачи (`job N started/completed/failed/cancelled`)
  и вывод процесса уходят вместе с отчётом о прогрессе.
- **боты Telegram и MAX:** хендлер logging + фоновая отправка раз в 5 секунд.
- **updater:** ход обновления (`update requested/applied/failed`) и logging.
- Служебный шум (`httpx`, `uvicorn.access`, `asyncio`) ниже WARNING не
  зеркалится — иначе отправка журнала порождала бы новые строки и поток
  зацикливался сам на себе. Предупреждения и ошибки проходят всегда.
- Все отказы запросов (`401/403/413/429`, `4xx/5xx` и необработанные исключения)
  теперь пишутся в журнал с дедупликацией 5 секунд — частая причина «не
  работает» видна сразу.

## Безопасность и доступ

`/api/logs*` остаётся административным: роли Telegram `operator`/`viewer`
получают `403`, доступ имеют API-ключ, парольная сессия, сервисный токен бота и
worker-токен. Сообщения очищаются от управляющих символов и обрезаются до 2000
символов; RTSP-секреты в журнал не попадают (у inference worker сообщения
проходят через существующий `redact_error`).

**Данные:** новая таблица не нужна — используется существующая `logs`
(добавлен индекс `ix_logs_service_timestamp`), срок хранения задан
`retention_days`.

---

# ZMK Vision v2.16.4 — переключатель предпросмотра MJPEG / MSE + авторелизы в main

## Режим предпросмотра на каждой камере

В настройках камеры (создание/редактирование) появился переключатель
**MSE · H.264** / **MJPEG · надёжный**. Значение хранится в базе отдельно для
каждой камеры и возвращается API (`preview_mode: "mse" | "mjpeg"`, по умолчанию
`"mse"` — текущее поведение не меняется).

- **MSE (по умолчанию):** H.264 через go2rtc, как было — 25–60 FPS при работающем go2rtc.
- **MJPEG:** надёжный multipart-JPEG поток `/api/cameras/{id}/mjpeg`, который
  фронтенд декодирует сам. Работает и при отключённом/недоступном go2rtc —
  запасной канал, который у вас стабильно работал.

Переключатель влияет **только на отображение в браузере**; RTSP-подключение
inference-воркера через go2rtc не затрагивается. Поле необязательное в
обновлении: старые клиенты, не знающие про него, не сбрасывают сохранённое
значение.

**backend:** `preview_mode TEXT NOT NULL DEFAULT 'mse'` (автомиграция колонки),
`CameraIn`/`CameraUpdate`, SELECT/INSERT/UPDATE камер, `bootstrap_env_camera`
остаётся на дефолте. **frontend:** тип `Cam.preview_mode`, переключатель в
модалке, `CameraPreview` ветка MJPEG-стрима с переподключением.

## Релизы снова появляются в main автоматически

Раньше релиз создавался только при ручном пуше тега `v*`, поэтому после v2.14.2
теги не появлялись, а версия проекта (VERSION) расходилась с последним релизом
GitHub. Теперь `.github/workflows/release.yml` срабатывает на **каждый push в
`main`**: читает `VERSION`, и если релиза `v{VERSION}` ещё нет — прогоняет полный
gate (pip-audit, ruff, bandit, pytest, сборку фронта и Docker) и публикует
релиз с архивами автоматически. Повторный push с той же версией ничего не
перевыпускает. Ручной push тега `v*` по-прежнему работает как запасной канал.

## Проверка

```bash
PYTHONPATH=backend pytest backend/tests -q        # preview_mode в CRUD и миграции
cd frontend && npm run lint && npm run build     # сборка проходит
pytest tests -q
```

---

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
