# ZMK Vision — Telegram & One-click Install release

## Установка
- **Windows 10/11:** скачайте ZIP, распакуйте и запустите `installers/install-windows.bat`.
- **Ubuntu/Debian:** скачайте TAR.GZ, распакуйте и выполните `bash installers/install-linux.sh`.

Установщики проверяют Docker, создают `.env`, опционально настраивают Telegram и запускают сервисы.

## Telegram
Бот включает whitelist/RBAC, `/status`, `/cameras`, `/events`, `/logs`, `/report`, `/models`, `/switch_model`, `/health`, inline-меню и Telegram Mini App `/telegram`.

> Для Mini App Telegram требует публичный HTTPS URL. Токены и RTSP-пароли не входят в release.
