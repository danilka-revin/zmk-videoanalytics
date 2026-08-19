# ZMK Vision: подробная инструкция для начинающих

Эта инструкция рассчитана на пользователя, который раньше не работал с Docker, терминалом, Telegram-ботами и серверными приложениями.

## 1. Что вы устанавливаете

ZMK Vision состоит из нескольких частей:

- **Web-панель** — основной интерфейс в браузере;
- **API** — сервер, который хранит камеры, события, настройки и отчёты;
- **Telegram-бот** — показывает состояние системы, события и отчёты;
- **Telegram Mini App** — мобильная версия панели внутри Telegram;
- **Docker Compose** — запускает все части проекта вместе.

После установки Web-панель будет доступна по адресу:

```text
http://localhost:5173
```

Документация API:

```text
http://localhost:8000/docs
```

> `localhost` означает «этот компьютер». С другого компьютера такой адрес не откроется.

---

## 2. Что понадобится

### Минимально

- 64-разрядная Windows 10/11 или Ubuntu/Debian Linux;
- 8 ГБ оперативной памяти;
- 10 ГБ свободного места;
- доступ в интернет на время установки;
- права администратора компьютера.

### Для настоящего распознавания видео

- RTSP-камеры;
- NVIDIA GPU;
- драйвер NVIDIA;
- веса обученной модели;
- размеченные изображения площадки.

Без камер и GPU проект всё равно запускается: в интерфейсе используются демонстрационные данные.

---

# Часть A. Установка на Windows 10/11

## 3. Скачивание проекта

1. Откройте страницу релизов:

   https://github.com/danilka-revin/zmk-videoanalytics/releases

2. Откройте последний релиз.
3. В разделе **Assets** скачайте файл вида:

   ```text
   zmk-videoanalytics-vX.Y.Z.zip
   ```

4. Нажмите правой кнопкой на ZIP-файле.
5. Выберите **Извлечь всё**.
6. Не запускайте установщик прямо из ZIP-архива. Сначала обязательно распакуйте весь архив.

## 4. Запуск установщика

В распакованной папке откройте каталог:

```text
installers
```

Дважды нажмите:

```text
install-windows.bat
```

Если Windows показывает предупреждение:

1. нажмите **Подробнее**;
2. проверьте имя файла;
3. нажмите **Выполнить в любом случае**.

Установщик:

1. проверит файлы проекта;
2. проверит Docker Desktop;
3. при необходимости установит Docker Desktop через `winget`;
4. запустит Docker;
5. создаст файл настроек `.env`;
6. предложит подключить Telegram;
7. соберёт контейнеры;
8. дождётся запуска API и Web-панели;
9. откроет интерфейс в браузере.

Установка Docker и первая сборка могут занять 5–20 минут.

## 5. Проверка Windows-установщика без установки

Откройте папку проекта в Проводнике. Нажмите в адресной строке, напишите `powershell` и нажмите Enter.

Выполните:

```powershell
.\installers\install-windows.ps1 -CheckOnly
```

Команда проверит структуру проекта и Docker Compose, но не запустит установку.

## 6. Если Docker Desktop просит перезагрузку

1. перезагрузите Windows;
2. запустите Docker Desktop;
3. дождитесь надписи **Docker Desktop is running**;
4. повторно запустите `install-windows.bat`.

Повторный запуск безопасен: существующие контейнеры будут обновлены.

---

# Часть B. Установка на Ubuntu/Debian

## 7. Скачивание и распаковка

Скачайте TAR.GZ из GitHub Releases или клонируйте репозиторий.

Вариант с Git:

```bash
git clone https://github.com/danilka-revin/zmk-videoanalytics.git
cd zmk-videoanalytics
```

Если репозиторий приватный, GitHub попросит авторизацию.

## 8. Проверка установщика

```bash
bash installers/install-linux.sh --check
```

Ожидаемый результат:

```text
Project files: OK
Installer validation: OK
```

## 9. Установка

```bash
bash installers/install-linux.sh
```

Введите пароль пользователя, если `sudo` его запросит. Символы пароля в терминале не отображаются — это нормально.

Установщик проверит или установит:

- Docker Engine;
- Docker Compose;
- системную службу Docker;
- контейнеры ZMK Vision.

## 10. Ошибка доступа к Docker

Если появляется `permission denied` для Docker:

```bash
sudo usermod -aG docker "$USER"
```

После этого выйдите из учётной записи Linux и войдите снова. До повторного входа можно использовать:

```bash
sudo docker compose up -d
```

---

# Часть C. Первый запуск

## 11. Как понять, что всё работает

Откройте:

```text
http://localhost:5173
```

Вы должны увидеть панель ZMK Vision.

Проверьте API:

```text
http://localhost:8000/api/health
```

Ожидается ответ примерно такого вида:

```json
{
  "status": "ok",
  "version": "1.3.0"
}
```

Swagger API:

```text
http://localhost:8000/docs
```

## 12. Основные разделы панели

- **Обзор** — показатели камер, GPU и событий;
- **Камеры** — список видеопотоков;
- **События** — журнал нарушений;
- **Модели** — версии AI-моделей и hot-swap;
- **Админ** — системные настройки, пользователи и логи;
- **Настройки** — пороги детекции.

Кнопка с палитрой в верхней панели открывает персонализацию:

- светлая, тёмная или системная тема;
- цвет интерфейса;
- плотность таблиц;
- компактное меню;
- API-ключ.

## 13. Тестовое событие

Нажмите кнопку **Тест события** в правом нижнем углу. Новая запись должна появиться в журнале событий.

Это проверяет цепочку:

```text
событие → API → база → Web-панель
```

---

# Часть D. Настройка Telegram

## 14. Создание бота

1. Откройте Telegram.
2. Найдите официального бота `@BotFather`.
3. Отправьте:

   ```text
   /newbot
   ```

4. Укажите отображаемое имя.
5. Укажите username, который заканчивается на `bot`.
6. BotFather выдаст token вида:

   ```text
   123456789:AAExampleToken
   ```

Никому не отправляйте этот token и не публикуйте его на GitHub.

## 15. Как узнать Telegram ID

Откройте `@userinfobot` или аналогичный сервис и получите числовой ID:

```text
123456789
```

## 16. Настройка `.env`

Файл `.env` находится в корне проекта. Если его нет, создайте копию `.env.example`.

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Linux:

```bash
cp .env.example .env
chmod 600 .env
```

Откройте `.env` в текстовом редакторе и заполните:

```env
TELEGRAM_BOT_TOKEN=123456789:AAExampleToken
TELEGRAM_ADMIN_IDS=123456789
TELEGRAM_OPERATOR_IDS=
TELEGRAM_VIEWER_IDS=
```

Несколько ID указываются через запятую без пробелов:

```env
TELEGRAM_OPERATOR_IDS=111111111,222222222
```

Роли:

- `admin` — полное управление;
- `operator` — события, логи и отчёты;
- `viewer` — только просмотр.

## 17. Запуск Telegram-бота

Windows или Linux:

```bash
docker compose --profile telegram up -d --build
```

Проверить лог:

```bash
docker compose logs --tail=100 telegram-bot
```

После запуска отправьте своему боту:

```text
/start
```

## 18. Telegram Mini App

Mini App нельзя нормально открыть через `localhost` на телефоне. Нужен публичный HTTPS-адрес, например:

```env
TELEGRAM_WEBAPP_URL=https://vision.example.ru/telegram
```

Для этого потребуется:

- домен;
- HTTPS-сертификат;
- reverse proxy;
- сетевой доступ к серверу.

Backend проверяет подпись Telegram `initData`, время авторизации и роль пользователя. Не добавляйте посторонние Telegram ID в whitelist.

---

# Часть E. Защита системы

## 19. API-ключ

Для локального знакомства ключ можно оставить пустым. Для сервера создайте длинный случайный ключ:

```env
ZMK_API_KEY=replace-with-a-long-random-value-of-at-least-32-characters
```

После включения ключа:

1. откройте Web-панель;
2. нажмите кнопку палитры;
3. найдите **Защищённый API**;
4. вставьте тот же ключ;
5. нажмите **Применить**.

Если ключ неверный, панель покажет ошибку получения данных.

## 20. Пароли инфраструктуры

Перед production-запуском замените:

```env
POSTGRES_PASSWORD=change-before-production
MINIO_ROOT_USER=zmkadmin
MINIO_ROOT_PASSWORD=change-before-production
```

Не используйте одинаковые пароли и не добавляйте `.env` в Git.

## 21. CORS

Если панель открывается через домен, укажите его:

```env
CORS_ORIGINS=https://vision.example.ru
```

Несколько адресов разделяются запятыми.

---

# Часть F. Камеры

## 22. RTSP URL

Пример:

```env
RTSP_CAM_01=rtsp://user:password@192.168.1.50:554/stream
```

Используйте только `rtsp://` или `rtsps://`.

Проверьте поток в VLC:

1. откройте VLC;
2. выберите **Медиа → Открыть URL**;
3. вставьте RTSP URL;
4. убедитесь, что видео воспроизводится.

RTSP-пароли не возвращаются через API списка камер, но всё равно должны храниться только в `.env` или secrets.

---

# Часть G. Диагностика

## 23. Состояние контейнеров

```bash
docker compose ps
```

У работающих сервисов должен быть статус `Up` или `healthy`.

## 24. Все последние логи

```bash
docker compose logs --tail=200
```

Лог отдельного сервиса:

```bash
docker compose logs --tail=200 api
docker compose logs --tail=200 web
docker compose logs --tail=200 telegram-bot
```

Наблюдение в реальном времени:

```bash
docker compose logs -f api
```

Выход из просмотра логов: `Ctrl+C`.

## 25. Перезапуск

```bash
docker compose restart
```

Полная пересборка:

```bash
docker compose up -d --build --remove-orphans
```

## 26. Порт уже занят

Если появляется ошибка `port is already allocated`, найдите программу, которая использует порт `5173` или `8000`.

Windows:

```powershell
netstat -ano | findstr :5173
netstat -ano | findstr :8000
```

Linux:

```bash
sudo ss -ltnp | grep -E ':5173|:8000'
```

Остановите конфликтующую программу или измените внешний порт в `docker-compose.yml`.

## 27. Панель открывается, но данных нет

Проверьте:

1. работает ли `http://localhost:8000/api/health`;
2. совпадает ли API-ключ;
3. нет ли ошибок в `docker compose logs api`;
4. правильно ли указан `CORS_ORIGINS`;
5. не повреждены ли настройки браузера.

Для сброса настроек интерфейса откройте персонализацию и нажмите **Сбросить**.

## 28. Telegram-бот молчит

Проверьте:

```bash
docker compose logs --tail=200 telegram-bot
```

Частые причины:

- неправильный token;
- Telegram ID отсутствует в whitelist;
- бот уже запущен на другом компьютере;
- API-контейнер недоступен;
- Mini App URL использует HTTP вместо HTTPS.

## 29. Очистка зависших контейнеров

```bash
docker compose down --remove-orphans
docker compose up -d --build
```

Не используйте `down -v`, если хотите сохранить данные.

---

# Часть H. Резервное копирование и обновление

## 30. Резервная копия

Перед обновлением остановите сервисы:

```bash
docker compose down
```

Скопируйте:

- `.env`;
- папку `data`;
- дополнительные конфигурации reverse proxy;
- сертификаты HTTPS.

## 31. Обновление через Git

```bash
git pull
docker compose up -d --build --remove-orphans
```

Проверьте:

```bash
curl http://localhost:8000/api/health
```

## 32. Обновление из ZIP/TAR.GZ

1. сделайте резервную копию `.env` и `data`;
2. скачайте новый релиз;
3. распакуйте его в новую папку;
4. верните `.env` и `data`;
5. запустите установщик новой версии.

---

# Часть I. Остановка и удаление

## 33. Остановить, сохранив данные

```bash
docker compose --profile telegram --profile production down --remove-orphans
```

## 34. Windows

```text
installers\uninstall-windows.bat
```

Полная очистка с подтверждением:

```powershell
.\installers\uninstall-windows.ps1 -Purge
```

## 35. Linux

Остановить и сохранить данные:

```bash
bash installers/uninstall-linux.sh
```

Полная очистка с подтверждением:

```bash
bash installers/uninstall-linux.sh --purge
```

Для удаления данных потребуется вручную написать `DELETE`.

---

# Часть J. Важные ограничения

Текущая версия предоставляет рабочие Web/API/Telegram интерфейсы и демонстрационный жизненный цикл AI. Она не включает реальные веса нейросети и не гарантирует показатели распознавания без:

- подключения RTSP;
- GPU worker;
- обученной модели;
- размеченного набора данных;
- проверки качества на конкретной площадке.

PostgreSQL, Redis и MinIO в production-профиле являются подготовленной инфраструктурой. Основной API текущей версии сохраняет данные в persistent SQLite.

---

## Короткая памятка

Запуск:

```bash
docker compose up -d --build
```

Запуск с Telegram:

```bash
docker compose --profile telegram up -d --build
```

Проверка:

```bash
docker compose ps
curl http://localhost:8000/api/health
```

Логи:

```bash
docker compose logs --tail=200
```

Остановка:

```bash
docker compose down
```

Web-панель:

```text
http://localhost:5173
```
