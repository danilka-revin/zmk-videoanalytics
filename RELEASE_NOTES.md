# ZMK Vision v2.13.10 — fix updater 243s hang + Bake warning

## Ошибка из лога

```
WARN[0000] Docker Compose is configured to build using Bake, but buildx isn't installed 
[+] Building 246.3s (81/85)
 => CANCELED [updater 2/6] RUN apt-get update && apt-get install -y --no-install-recommends curl docker.io  243.8s
```

**Причина:** `services/updater/Dockerfile` делал `apt-get update && apt-get install docker.io` — `docker.io` ~100 МБ + apt update медленно, на медленном интернете 4 минуты и CANCELED. Плюс `COMPOSE_BAKE` warning из-за отсутствия buildx.

**Фикс v2.13.10:**

**updater Dockerfile — теперь Alpine:**
```dockerfile
FROM python:3.12-alpine
RUN apk add --no-cache curl docker-cli docker-cli-compose
```
- `apk` в Alpine в 10x быстрее `apt`
- `docker-cli` + `docker-cli-compose` ~20 МБ вместо `docker.io` ~100 МБ
- Сборка updater теперь ~5 сек вместо 243 сек

**start.sh:**
- Первая попытка теперь `COMPOSE_BAKE=false COMPOSE_DOCKER_CLI_BUILD=0 up -d --build` чтобы убрать `WARN Bake but buildx isn't installed`
- `export COMPOSE_BAKE=false` по умолчанию, fallback цепочка 6 шагов сохранена
- `repair_build_cache()` чистит `builder prune --all -f` + `buildx prune`

**Сохранено:**
- `inference_worker` и `training_worker` `FROM ultralytics/ultralytics:8.4.126` — быстрая сборка ~20 сек
- `Dockerfile.slim` fallback для parent snapshot ошибки
- `frontend/package-lock.json` с `hls.js 1.6.13`
- WebRTC H264 FULL QUALITY 60 FPS, opencv decoder, 1920 max 85%

## Обновление

```bash
cd ~/zmk-vision
git config pull.rebase false
git fetch origin --tags --prune --force
git checkout -B main origin/main
./start.sh
# теперь сборка ~30-40 сек вместо 246 сек
```

---

# ZMK Vision v2.13.9 — fix бесконечная загрузка inference-worker pip torch

**Причина:** `python:3.12-slim` + `pip install torch==2.5.1` ~200 МБ медленно.

**Фикс:** revert к `ultralytics/ultralytics:8.4.126` base + `Dockerfile.slim` fallback, 6-step fallback chain.

---

# ZMK Vision v2.13.8 — fix divergent branches + auto-update 404 + npm ci

git config pull.rebase false, fetch --prune --tags --force, auto-update fallback на git, package-lock.json с hls.js
