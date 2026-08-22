# ZMK Vision v2.11.0 — One-click startup + first-run setup wizard on Linux

## Запуск в один клик (Ubuntu / Debian)

Теперь весь проект запускается **одной командой** — `./start.sh`:
- **При первом запуске** открывается мастер настройки (тот самый выбор, который раньше не появлялся при запуске `start.sh`): мессенджер — Telegram / MAX / **«без бота»**, токены бота, включение real-time inference / training воркеров, токены безопасности.
- **При повторных запусках** — без вопросов, сразу старт.
- Конфигурация сохраняется в `.env` и `.zmk-profiles`.

### Почему раньше «выбор с ботом или без» не появлялся
`start.sh` был просто лаунчером и никогда не опрашивал конфигурацию (выбор был только в `install-linux.sh`, и он пропускался при имеющемся `.env`). Теперь мастер вынесен в общий `installers/wizard.sh`, который вызывается **и** `start.sh` **и** `install-linux.sh`, пока конфигурация не задана. Больше ничего не нужно заполнять вручную.

## Что добавлено

- `installers/wizard.sh` — общий мастер настройки (мессенджер / без бота, токены, воркеры), переиспользуемый `start.sh` и установщиком.
- `start.sh` — умный лаунчер: первый запуск → мастер, далее → просто старт. Флаги `--setup` (повторить мастер) и `--no-update`.
- `install.sh` — простой синоним одного клика (передаёт в `start.sh`).
- `installers/create-desktop.sh` — создаёт ярлык **ZMK Vision** на рабочий стол/в меню Ubuntu, чтобы запускать двойным кликом.
- `installers/install-linux.sh` — рефакторинг: использует общий мастер, поддерживает `--setup` (только настройка) и `--check`.

## Дополнительные команды

```bash
./start.sh                 # запуск (первый раз — мастер)
./start.sh --setup         # переоткрыть мастер настройки
bash installers/create-desktop.sh   # создать ярлык рабочего стола
NONINTERACTIVE=1 MESSENGER_PROVIDER=none ENABLE_INFERENCE=true ./start.sh  # без вопросов
```

## Проверки

- Backend **58/58**, установщики/updater/worker **27/27** (добавлены тесты: синтаксис новых скриптов, первый запуск вызывает мастер и пишет `.env`/`.zmk-profiles` даже без docker), Telegram 3/3, MAX 3/3.
- Ruff, Bandit (прод+updater), tsc/lint, `npm audit` (0), pip-audit (0), shell syntax, `git diff --check` — чисто.

## Примечание

Нужен установленный Docker Engine и Compose plugin. Если их нет — `./start.sh` подскажет команду установки (или используйте `installers/install-linux.sh`, который ставит их сам для Ubuntu/Debian).
