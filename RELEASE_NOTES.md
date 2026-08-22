# ZMK Vision v2.11.4 — Accept "recovering" status + explicit RTSP socket timeout

## Что исправлено

### 1. Телеметрия отклонялась с 422 (главное)
Воркер после временного сбоя потока шлёт статус **`recovering`**, но модель телеметрии на API принимала только `online/offline/error/unknown`. В результате POST `/api/cameras/{id}/telemetry` возвращал **`422 Unprocessable Entity`**, статус камеры не обновлялся, и в логе висело:
```
Client error '422 Unprocessable Entity' for url '/api/cameras/.../telemetry'
```
**Решение:** в `CameraTelemetry.status` добавлен `recovering`. Теперь камера корректно показывает «Восстановление», а панель уже умеет его отображать.

### 2. Зависание потока (~30 сек) в контейнере
В логе было `Stream timeout triggered after 30002 ms` — это таймаут RTSP-сокета FFmpeg **по умолчанию ~30 секунд**, а не сеть.
**Решение:** теперь воркер передаёт явный сокетный таймаут `stimeout` через **правильный** синтаксис `OPENCV_FFMPEG_CAPTURE_OPTIONS`:
```
rtsp_transport;tcp|stimeout;5000000[|buffer_size;N]
```
(параметры разделяются `|`, а НЕ запятой — запятая ломала парсинг `rtsp_transport`, как в v2.11.3). Опция `stimeout` устраняет зависание и ускоряет переподключение при сбое.

### Подтверждено на реальном потоке
- Поток открывается и **читается стабильно 45 секунд подряд** через `rtsp_transport;tcp|stimeout;5000000` (826 кадров, 0 таймаутов). Камера, сеть и ссылка полностью исправны.
- Воркер логирует `OPENED via tcp`, затем инференс и снапшоты.

## Параметры (.env)
```env
RTSP_TRANSPORT=auto      # auto | tcp | udp
RTSP_STIMEOUT=5000000    # сокет-таймаут FFmpeg, мс
RTSP_BUFFER_SIZE=        # опционально
```

## Проверки
- Backend **61/61** (добавлен тест: телеметрия принимает `recovering`, камера отображает его), установщики/updater/worker **28/28**, Telegram 3/3, MAX 3/3.
- Ruff, Bandit (прод+updater), tsc/lint, `npm audit` (0), pip-audit (0), `git diff --check` — чисто.
- Реальный поток прочитан без таймаутов; синтаксис опции проверен.

## Как применить

```bash
# пересобрать и поднять воркер инференса
docker compose --profile inference up -d --build --remove-orphans
# смотреть логи
docker compose --profile inference logs --tail=100 inference-worker
```

После пересборки камера должна перейти в `online` (или `recovering` при кратком сбое) и показывать кадры.
