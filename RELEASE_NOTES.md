# ZMK Vision v2.11.3 — Fix RTSP stream never opening (worker option bug)

## Симптом

Камера работает в VLC, но на сайте «не подключена». В логах inference-воркера тысячи строк вида:
```
[Eval @ ...] Invalid chars ',stimeout;5000000' at the end of expression 'tcp,stimeout;5000000'
[RTSP demuxer] Unable to parse "rtsp_transport" option value "tcp,stimeout;5000000"
[RTSP demuxer] Error setting option rtsp_transport to value tcp,stimeout;5000000.
inference: camera ... FAILED to open via tcp ...
inference: camera ... FAILED to open via udp ...
```

## Причина (баг воркера, не камеры)

Воркер передавал FFmpeg опцию вида `rtsp_transport;tcp,stimeout;5000000`. Парсер RTSP-демуксера ждёт **чистое** значение (`tcp` / `udp`), а наличие `,stimeout;...` в конце он считает ошибкой и **отклоняет всю опцию**. В результате **обе** попытки (tcp и udp) падали на парсинге, и поток камеры **вообще никогда не открывался** — независимо от транспорта и камеры. Это было привнесено в прошлом фиксе транспорта.

## Решение

- Воркер теперь генерирует **чистую** опцию: `rtsp_transport;tcp` (или `;udp`) — без `,stimeout;...`.
- Таймауты уже задаются через `CAP_PROP_OPEN_TIMEOUT_MSEC` / `CAP_PROP_READ_TIMEOUT_MSEC`, отдельный `stimeout` не нужен.
- Опциональный `RTSP_BUFFER_SIZE` при необходимости добавляется отдельно (только если задан пользователем).

## Проверка (подтверждено на реальном потоке)

- Подтверждено, что именно такая опция раньше роняла парсер (`Invalid chars ',stimeout;5000000'`).
- С исправленной опцией реальный RTSP поток из песочницы **открывается и читает кадр** по обоим транспортам:
  - `tcp: isOpened=True read=True shape=(1440,2560,3)`
  - `udp: isOpened=True read=True shape=(1440,2560,3)`
- Тест воркера теперь проверяет, что опция = `rtsp_transport;tcp` и не содержит `stimeout`/`buffer_size` (защита от регресса).
- Backend **60/60**, установщики/updater/worker **28/28**, Telegram 3/3, MAX 3/3; Ruff, Bandit, tsc/lint, `npm audit` (0), pip-audit (0), `git diff --check` — чисто.

## Как применить (главное — пересобрать воркер)

```bash
# пересобрать и поднять воркер инференса
docker compose --profile inference up -d --build --remove-orphans

# посмотреть логи — теперь должно быть "OPENED via tcp"
docker compose --profile inference logs --tail=80 inference-worker
```

После пересборки воркер откроет поток корректной опцией, и камера покажет кадры.

## Примечание

Если хотите зафиксировать транспорт — `RTSP_TRANSPORT=tcp` или `=udp` в `.env`. По умолчанию `auto` (tcp, при неудаче udp). Опция `RTSP_BUFFER_SIZE` — только если понадобится под конкретную камеру.
