# ZMK Vision v2.14.1 — fix updater: local changes + untracked Dockerfile.slim blocking checkout

## Ошибка пользователя при обновлении на v2.14.0

```
 * tag               v2.14.0    -> FETCH_HEAD
error: Ваши локальные изменения в указанных файлах будут перезаписаны при переключении на состояние:
    RELEASE_NOTES.md
    VERSION
    services/inference_worker/Dockerfile
    services/training_worker/Dockerfile
    services/updater/Dockerfile
    start.sh
Сделайте коммит или спрячьте ваши изменения перед переключением веток.
error: Указанные неотслеживаемые файлы в рабочем каталоге будут перезаписаны при переключении на состояние:
    services/inference_worker/Dockerfile.slim
    services/training_worker/Dockerfile.slim
Переместите эти файлы или удалите их перед переключением веток.
Прерываю
fatal: «origin/v2.14.0» не является коммитом, поэтому невозможно создать из него ветку «main»
```

**Причины:**

1. **Локальные изменения** — пользователь или предыдущий auto-update оставил измененные `VERSION`, `RELEASE_NOTES.md`, `Dockerfile` (например из-за `git pull` с конфликтами). `git checkout -B main origin/main` отказывается перезаписывать измененные файлы без `reset --hard`.

2. **Неотслеживаемые файлы** — `Dockerfile.slim` появились в v2.13.10 как fallback для parent snapshot ошибки, но если пользователь был на старой версии и имел локальные правки, `checkout` отказывается перезаписывать неотслеживаемые файлы которые теперь отслеживаются.

3. **Неправильная команда** — пользователь пытался `git checkout -B main origin/v2.14.0` (тег), но `origin/v2.14.0` не ветка, а тег `v2.14.0`. Правильно `origin/main` или `git checkout v2.14.0`.

## Фикс v2.14.1 — неубиваемый апдейтер

**installers/auto-update.sh:**
- Перед любым `fetch`/`checkout`: `git reset --hard HEAD` + `git clean -fd` — сбрасывает локальные правки VERSION, RELEASE_NOTES, Dockerfile и удаляет неотслеживаемые Dockerfile.slim
- `fetch --depth=1 origin "$latest"` теперь fallback на полный `fetch origin "$latest" --prune --tags`
- Checkout пробует: `FETCH_HEAD` → `origin/main` → тег → `origin/$latest` — покрывает и теги и ветки
- `safe.directory` + `pull.rebase false` + `pull.ff only` перед всем

**installers/bootstrap-linux.sh:**
- Тот же `reset --hard` + `clean -fd` перед fetch и checkout
- Checkout теперь пробует тег тоже: `checkout -B main origin/main` как primary, затем `origin/$REF`, `$REF`, и прямой `checkout $REF` для тегов
- Фикс для `origin/v2.14.0` → теперь правильно чекаутит main

**start.sh:**
- `repair_build_cache()` теперь также `pull.ff only` (было только rebase)

**Сохранено из v2.14.0:**
- Single connection via go2rtc: камера → go2rtc (1 conn) → inference RTSP `rtsp://host.docker.internal:8554/zmk-{id}` (25-60 FPS) + browser WebRTC H264 direct (25-60 FPS)
- Frontend WebRTC 8s timeout, 4 STUN, exponential backoff, MSE fMP4 over WS, HLS lowLatency
- Backend go2rtc sync с `transport=tcp` + `mp4`, 5 STUN
- Inference worker `using_go2rtc`, `go2rtc_failures`, fallback на direct после 5 fails

## Ручное обновление если застряли (100% рабочий способ)

```bash
cd ~/zmk-vision
git config --global --add safe.directory ~/zmk-vision
sudo git config --global --add safe.directory ~/zmk-vision
git reset --hard HEAD
git clean -fd
git fetch origin --tags --prune --force
git checkout -B main origin/main
./start.sh
# теперь 2.14.1, дальше auto-update неубиваемый
```

Или через bootstrap (тоже теперь с reset+clean):

```bash
curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh | bash
```

## Проверка

```bash
cd ~/zmk-vision
cat VERSION
# 2.14.1
docker logs zmk-vision-inference-worker-1 --tail 20
# должно быть: "opened via GO2RTC RTSP TCP (single connection to camera, true FPS)"
```

---
# v2.14.0 — ПОЛНЫЙ РЕДИЗАЙН СТРИМИНГА: single connection via go2rtc, true 25-60 FPS
- go2rtc единственный RTSP клиент к камере, inference и браузер — клиенты go2rtc
- Inference: 4.2 → 25-60 FPS локально rtsp://host.docker.internal:8554/zmk-{id}
- Preview: 4.2 → 25-60 FPS WebRTC H264 passthrough как в VLC
- Frontend: WebRTC 8s + MSE + HLS fallback, exponential backoff, 15s health check
---
# v2.13.12 — fix auto-update dubious ownership + 404
# v2.13.11 — fix 4 FPS MJPEG bottleneck
# v2.13.10 — updater Alpine 5s vs 243s
