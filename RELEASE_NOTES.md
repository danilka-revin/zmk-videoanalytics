# ZMK Vision v1.2.3 — Beginner Guide

## Новое
- Добавлена подробная русскоязычная инструкция для начинающих: `docs/BEGINNER_GUIDE_RU.md`.
- Пошагово описаны установка на Windows и Linux, первый запуск, Docker, Telegram, Mini App, API key, RTSP, диагностика, резервное копирование, обновление и удаление.
- В README добавлена заметная ссылка на руководство.

## Исправлено
- Пустой `TELEGRAM_WEBAPP_URL` теперь корректно переключает Compose на локальный fallback вместо фиктивного домена.
- Windows и Linux установщики всегда обновляют значение Mini App URL без накопления устаревших параметров.
- Проверки установщиков, Windows PowerShell CI и release gates сохранены.

## Установка
- Windows 10/11: распакуйте полный ZIP и запустите `installers/install-windows.bat`.
- Ubuntu/Debian: распакуйте полный TAR.GZ и выполните `bash installers/install-linux.sh`.
- Проверка без установки: `bash installers/install-linux.sh --check` или `install-windows.bat -CheckOnly`.

Начинающим рекомендуется сначала открыть `docs/BEGINNER_GUIDE_RU.md`.
