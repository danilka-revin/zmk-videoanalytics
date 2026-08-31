# ZMK Vision v2.13.8 — fix divergent branches + auto-update 404 + npm ci

## Ошибки из лога пользователя — исправлено

```
hint: You have divergent branches and need to specify how to reconcile them.
fatal: Need to specify how to reconcile divergent branches.
[auto-update] Current: 2.13.5  |  Latest: 2.13.7
curl: (22) The requested URL returned error: 404
ERROR: download failed: https://github.com/danilka-revin/zmk-videoanalytics/releases/download/v2.13.7/zmk-videoanalytics-v2.13.7.tar.gz
=> ERROR [web build 4/6] RUN npm ci
Missing: hls.js@1.6.13 from lock file
```

**Причины:**
1. `git pull` падал с `divergent branches` — не был настроен `pull.rebase`
2. `v2.13.7` релиз был создан вручную `gh release create` без артефактов, поэтому auto-update получал 404. Release workflow теперь успешно загрузил `tar.gz` + `SHA256SUMS.txt`
3. `v2.13.5` всё ещё использовал `ultralytics/ultralytics:8.4.126` base с битым parent snapshot

**Фикс v2.13.8:**

**bootstrap-linux.sh:**
- `git config pull.rebase false` + `pull.ff only` перед fetch
- `fetch --prune --tags --force` + fallback `fetch origin main`
- `checkout -B` с fallback на `origin/$REF`

**auto-update.sh:**
- Если tar.gz не скачался (404) — fallback на git: `fetch --prune --tags --force` + `checkout -B main FETCH_HEAD`
- SHA256 проверка теперь не фаталит сразу, а пробует git fallback
- Логи `tarball unavailable, trying git fetch`

**start.sh:**
- `git config pull.rebase false` в начале, чтобы ручной `git pull` не падал с fatal
- `repair_build_cache()` теперь `builder prune --all -f` + `git fetch --prune --tags --force` для починки divergent

**Сборка:**
- `frontend/package-lock.json` уже содержит `hls.js 1.6.13` (v2.13.7)
- `services/inference_worker/Dockerfile` и `training_worker/Dockerfile` `FROM python:3.12-slim` (v2.13.6)

## Как починить текущую установку (если застрял на 2.13.5)

```bash
cd ~/zmk-vision
git config pull.rebase false
git fetch origin --tags --prune --force
git checkout -B main origin/main
git pull origin main
./start.sh --no-update
# или полная переустановка одной командой:
bash <(curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh)
```

## Сохранено

- WebRTC H264 FULL QUALITY 60 FPS no-cut, `opencv` decoder default, `next_frame_at 0.001`, `_encode_live` 1920 max 85%, model `image.copy()` full
- Backend `camera_mjpeg` go2rtc proxy first
- Frontend `webrtc → hls (hls.js) → go2rtc-mjpeg → api-mjpeg → snapshot`
- Docker BuildKit fallback chain `COMPOSE_BAKE=false DOCKER_BUILDKIT=0 --no-cache`

---

# ZMK Vision v2.13.7 — fix npm ci hls.js lock file

## Ошибка сборки web — исправлено

```
=> ERROR [web build 4/6] RUN npm ci
npm error Missing: hls.js@1.6.13 from lock file
```

**Фикс:** `frontend/package-lock.json` обновлён — добавлен `hls.js 1.6.13`

## Сохранено из v2.13.6

- Docker BuildKit parent snapshot fix `python:3.12-slim`
- WebRTC H264 FULL QUALITY 60 FPS

Результат: сборка проходит, `● WEBRTC H264 FULL QUALITY` 25-30 FPS без `snapshot fallback`.
