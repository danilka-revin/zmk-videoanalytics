# ZMK Vision v2.13.0 — настоящий live как в VLC + все нововведения

## Главное исправление: камера больше не слайд-шоу

Предыдущая сборка падала в snapshot fallback (3 сек кадр) из-за двух багов:
- фронтенд запрашивал `src=<camera_id>` у go2rtc, а API регистрировал `zmk-<camera_id>` → WebRTC 404;
- `CAMERA_LIVE_FPS=0` отключал MJPEG даже когда go2rtc недоступен.

Исправлено:
- **WebRTC теперь запрашивает `zmk-<id>`** (совпадает с тем, что создаёт API), добавлен второй STUN, bundlePolicy, retry-логика и таймаут 5 сек до fallback;
- **API создаёт оба имени** `zmk-<id>` и `<id>` для совместимости, чистит stale `cam_*` и `zmk-*`;
- **inference-worker публикует MJPEG даже при включённом go2rtc** — `CAMERA_LIVE_FPS` по умолчанию теперь `25` (в `.env.example` и `docker-compose.yml`), а не `0`. Это даёт реальный 25 FPS fallback как в VLC, если WebRTC сигнал недоступен. Установите `0`, чтобы полностью отключить MJPEG и экономить CPU;
- **go2rtc.yaml** настроен для низкой задержки как в VLC: TCP транспорт, `nobuffer`, `avioflags=direct`, `max_delay 100ms`, кандидаты STUN, host-network 1984/8555;
- **nginx** уже проксирует `/rtc/` без буферизации, WebSocket upgrade, CSP позволяет `blob:` для JPEG.

Результат: в карточке камеры теперь **настоящий live H.264 через WebRTC** (● WEBRTC REAL-TIME), а при сбое go2rtc — **живой MJPEG 25 FPS**, а не слайд-шоу из снапшотов.

---

## Все нововведения, собранные в main (с v2.12.0 → v2.13.0)

### Камеры и стриминг
- go2rtc + WebRTC как основной транспорт: API зеркалит камеры в go2rtc, браузер получает H.264 напрямую, без перекодирования каждого кадра в MJPEG;
- persistent FFmpeg decoder по умолчанию: `-fflags +discardcorrupt`, `-err_detect ignore_err`, `nobuffer`, `avioflags=direct`, `low_delay`, jitter 100 мс;
- машина состояний `connecting → online / recovering / offline`, heartbeat worker → API, реальный FPS по окну телеметрии;
- TCP-first / UDP fallback в `RTSP_TRANSPORT=auto`, таймауты открытия/чтения до `open()`, ожидание keyframe 15 сек;
- карточки одинакового размера `Компакт / Обычные / Крупные`, native fullscreen, честный FPS `факт / лимит` до 60 FPS;
- кнопка «Перезапустить» в карточке, `RTSP_CAM_01` bootstrap.

### Модели и AI-конвейер
- мульти-модельный пайплайн: слоты `people / helmet / workwear / phone / smoking / zone`, каждая роль — отдельная модель;
- загрузка локальных артефактов из web до 2 GB (ONNX / PyTorch / TensorRT), стриминг через nginx без буферизации;
- безопасный жизненный цикл теста модели на камере, self-heal one-command launch, отображение состояния worker;
- нормализация кастомных классов с кириллицей («Человек», «Без каски», «Жилет» → `person / no_helmet / vest`), исправлены `without_hardhat` токены;
- draw overlay: все детекции рисуются на live кадре, ASCII fallback для кастомных имён;
- отдельный `CAMERA_INFERENCE_FPS` (8 FPS) чтобы медленная модель не тормозила превью.

### Безопасность и доступ
- парольная защита `ZMK_PASSWORD_AUTH=true`, начальный пароль `1234`, смена в Персонализация → Доступ, сессии 12ч, email восстановление через SMTP;
- hardened account controls, support bundle, event evidence zip с кадрами, smart search с опечатками и кириллицей;
- `X-API-Key` + `X-Worker-Token` разделены, bot tokens в приватном volume `.bot-tokens`.

### Мессенджеры и Mini App
- админка ботов: токены вводятся в Admin → Боты, хранятся в `bot-token-data` volume, не в SQLite;
- Telegram Mini App полностью тёмная, поддержка @username для ролей, расширенные операции;
- tokenless MAX setup assistant, роли admin/operator/viewer, алерты по severity;
- экспорт отчётов: overview как evidence archive, event reports с фреймами.

### Эксплуатация одной командой
- `installers/bootstrap-linux.sh` — клон/fast-forward, миграция legacy launcher `ZMK_REF=main` → текущая ветка, `.zmk-ref` трекинг, `zmk-vision` команда;
- `frontend/Dockerfile` рендерит `GO2RTC_UPSTREAM` через `docker-entrypoint.sh` + `envsubst`, `nginx -t` в отдельной RUN-слое (требование тестов);
- `docker-compose.yml` — `go2rtc` host network 1984/8555, `training-worker` always-on CPU/GPU fallback, `model-data` shared, `updater` sidecar;
- `install.sh` / `start.sh` проверяют `bash -n`, `--check` dry-run.

### Проверки релиза
- backend 113 тестов, integration 52 passed 1 skipped;
- `ruff`, `bandit`, `pip-audit`, `npm audit`, `npm run build`;
- `docker compose --profile inference config` и build всех образов в CI.

## Обновление

```bash
git pull
# или одной командой:
bash <(curl -fsSL https://raw.githubusercontent.com/danilka-revin/zmk-videoanalytics/main/installers/bootstrap-linux.sh)
docker compose --profile inference up -d --build --force-recreate api web go2rtc inference-worker
docker compose logs -f --tail=200 inference-worker go2rtc
```

Ожидаемый лог worker:

```
inference: camera runtime started (api=http://api:8000, device=auto, transport=auto, decoder=ffmpeg)
inference: camera cam_... opened via TCP
inference: camera cam_... received first decoded frame
```

В браузере карточка должна показать `● WEBRTC REAL-TIME` или `● LIVE MJPEG fallback` с плавным видео, а не `snapshot fallback` каждые 3 сек.
