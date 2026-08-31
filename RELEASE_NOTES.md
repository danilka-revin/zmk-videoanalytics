# ZMK Vision v2.13.12 — fix auto-update dubious ownership + 404 tarball fallback

## Ошибка из лога пользователя (v2.13.10 → v2.13.11)

```
fatal: not in a git directory
fatal: detected dubious ownership in repository at '/root/zmk-vision'
To add an exception for this directory, call:
    git config --global --add safe.directory /root/zmk-vision
[auto-update] Current: 2.13.10  |  Latest: 2.13.11
[auto-update] New version 2.13.11 detected. Downloading...
curl: (22) The requested URL returned error: 404
ERROR: download failed: https://github.com/danilka-revin/zmk-videoanalytics/releases/download/v2.13.11/zmk-videoanalytics-v2.13.11.tar.gz (will try git fallback)
[auto-update] Tarball unavailable, trying git fetch for v2.13.11...
fatal: detected dubious ownership in repository at '/root/zmk-vision'
ERROR: git fallback also failed
```

**Две проблемы:**

1. **dubious ownership** — когда `/root/zmk-vision` создан одним пользователем (или через `sudo`), а `git` запускается от другого UID (root vs 1000), Git отказывается работать без `safe.directory`. Все `git -C` команды в `auto-update.sh`, `bootstrap-linux.sh`, `start.sh` падали.

2. **404 tarball race** — GitHub API возвращает `latest` тег сразу после `git push --tags`, но assets (`tar.gz`, `zip`) загружаются Release workflow'ом только через 3-4 минуты. Пользователь, запустивший `./start.sh` в этот промежуток, получал 404 и падал в git fallback, который тоже был сломан из-за (1).

## Фикс v2.13.12

**installers/auto-update.sh:**
- Добавлен `zmk_ensure_safe_git()` — перед ЛЮБОЙ git операцией:
```bash
git config --global --add safe.directory "$root"
sudo git config --global --add safe.directory "$root"
sudo -u "$SUDO_USER" git config --global --add safe.directory "$root"
```
- Вызывается в `zmk_check_and_update` до `zmk_current_version`, и перед обоими git fallback путями (fetch tag и fetch main)
- Теперь даже если tarball 404, git fallback сработает

**start.sh:**
- Перед `git config pull.rebase` и `git branch --show-current` добавлен `safe.directory $(pwd)` + sudo версия
- Исправляет `fatal: not in a git directory` / `dubious ownership` при запуске из `/root/zmk-vision`

**installers/bootstrap-linux.sh:**
- Добавлен тот же `zmk_ensure_safe_git()` и вызовы перед `branch --show-current`, `remote get-url`, `status`, `fetch`, `config`

**installers/install-linux.sh:**
- `safe.directory` перед `git branch --show-current` / `rev-parse` в `print_install_summary`

**Сохранено из v2.13.11:**
- Worker больше НЕ делает JPEG re-encode когда `GO2RTC_ENABLED=true` — true 25-60 FPS WebRTC H264 как в VLC
- Frontend WebRTC пробует оба имени `zmk-{id}` и `{id}`, timeout 6s, 3 STUN
- Backend `camera_mjpeg` 503 fast-fail + 5s idle timeout
- go2rtc.yaml `default_query: mp4`

## Обновление вручную (если auto-update застрял на 2.13.10)

```bash
cd /root/zmk-vision
git config --global --add safe.directory /root/zmk-vision
sudo git config --global --add safe.directory /root/zmk-vision
git config pull.rebase false
git fetch origin --tags --prune --force
git checkout -B main origin/main
./start.sh
# теперь на 2.13.12, дальше auto-update работает
```

Или:

```bash
curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh | bash
```

---
# v2.13.11 — fix 4 FPS MJPEG bottleneck → WebRTC H264 25-60 FPS
- inference_worker skips _encode_live when go2rtc enabled, true FPS 25-60
- backend camera_mjpeg 503 fast-fail
- frontend WebRTC tries both names, 6s timeout, 3 STUN
---
# v2.13.10 — updater Alpine fix (243s hang)
---
# v2.13.9 — inference-worker pip torch fix
