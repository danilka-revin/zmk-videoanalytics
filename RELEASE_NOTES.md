# ZMK Vision v2.13.9 — fix бесконечная загрузка inference-worker pip torch

## Ошибка из лога

```
=> CANCELED [inference-worker 6/7] RUN pip install --no-cache-dir ultralytics==8.4.126 opencv-python-headless==4.10.0.84 torch==2.5.1 ...
65.7s бесконечная загрузка как исправить
[+] Building 165.1s (78/83)
```

**Причина:** в v2.13.6 мы перешли с `FROM ultralytics/ultralytics:8.4.126` на `FROM python:3.12-slim` + `pip install torch` чтобы избежать `parent snapshot does not exist`. Но `torch==2.5.1` CPU ~200 МБ + torchvision + ultralytics = 5-10 минут сборки на медленном интернете, выглядит как зависание.

**Фикс v2.13.9 — быстрый + надёжный:**

**Dockerfiles:**
- `services/inference_worker/Dockerfile` и `training_worker/Dockerfile` снова `FROM ultralytics/ultralytics:8.4.126` — быстрая сборка, всё уже внутри (torch, opencv, ultralytics)
- Добавлен `Dockerfile.slim` как fallback: `FROM python:3.12-slim` + pip torch, используется только если ultralytics base падает с parent snapshot ошибкой
- `ffmpeg` ставится через apt, `httpx` через pip

**start.sh fallback цепочка теперь 6 шагов:**
1. `up -d --build` обычный
2. `COMPOSE_PARALLEL_LIMIT=1 up -d --build` — избегает race
3. `COMPOSE_BAKE=false COMPOSE_DOCKER_CLI_BUILD=0 up -d --build` — без Bake
4. `DOCKER_BUILDKIT=0 COMPOSE_BAKE=false build --no-cache inference-worker training-worker` + `up -d`
5. `builder prune --all -f` + `DOCKER_BUILDKIT=0 --no-cache --parallel 1`
6. **NEW:** slim fallback — `docker build -f Dockerfile.slim -t zmk-vision-inference-worker` + `up -d --no-build`, если не помогло — временно подменяет Dockerfile на slim и `up -d --build`

Результат: обычная сборка ~20-30 секунд (ultralytics base), а не 165 секунд. Если BuildKit падает с `parent snapshot does not exist` — автоматически чистит кэш и пробует classic builder, в крайнем случае slim.

## Сохранено из v2.13.8

- `bootstrap-linux.sh`: `git config pull.rebase false`, `fetch --prune --tags --force`, fallback checkout
- `auto-update.sh`: fallback на git если tar.gz 404
- `start.sh`: `git config pull.rebase false` в начале
- `frontend/package-lock.json` содержит `hls.js 1.6.13` — `npm ci` проходит
- WebRTC H264 FULL QUALITY 60 FPS, `opencv` decoder default, `CAMERA_LIVE_FPS=60`

## Обновление

```bash
cd ~/zmk-vision
git config pull.rebase false
git fetch origin --tags --prune --force
git checkout -B main origin/main
./start.sh
```

---

# ZMK Vision v2.13.8 — fix divergent branches + auto-update 404 + npm ci

## Ошибки

```
hint: You have divergent branches...
curl: (22) 404 .../v2.13.7.tar.gz
Missing: hls.js@1.6.13 from lock file
```

**Фикс:** git config pull.rebase false, fetch --prune --tags --force, auto-update fallback на git, package-lock.json с hls.js

---

# ZMK Vision v2.13.7 — fix npm ci hls.js lock file

`frontend/package-lock.json` обновлён — добавлен `hls.js 1.6.13`
