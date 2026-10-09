#!/usr/bin/env bash
# =====================================================================
# Zovod — сброс паролей ВСЕХ панельных пользователей одной командой.
#
#   bash set-passwords.sh              → пароль 72327232 для всех аккаунтов
#   bash set-passwords.sh МОЙ_ПАРОЛЬ   → свой пароль для всех аккаунтов
#
# Работает с локальной базой data/videoanalytics.db. Если базы ещё нет —
# скрипт ничего не ломает: при первом запуске аккаунты admin и director
# получат пароль из deploy.env.
# =====================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASSWORD="${1:-72327232}"
DB="${VIDEOANALYTICS_DB:-$ROOT/data/videoanalytics.db}"

if [[ ! -f "$DB" ]]; then
  echo "База $DB ещё не создана — просто запустите систему (./start.sh):"
  echo "первые аккаунты получат пароль из deploy.env ($PASSWORD)."
  exit 0
fi

python3 - "$DB" "$PASSWORD" <<'PY'
import base64, hashlib, secrets, sqlite3, sys

db_path, password = sys.argv[1], sys.argv[2]

def hash_password(pw: str) -> str:
    # Точно повторяет формат хэша backend/app/main.py (_hash_password).
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 260_000)
    return (
        "pbkdf2_sha256$260000$"
        + base64.urlsafe_b64encode(salt).decode()
        + "$"
        + base64.urlsafe_b64encode(digest).decode()
    )

con = sqlite3.connect(db_path)
try:
    rows = [str(r[0]) for r in con.execute("SELECT login FROM auth_accounts ORDER BY login")]
    if not rows:
        print("Таблица пользователей пуста — запустите систему, затем повторите.")
        sys.exit(0)
    for login in rows:
        con.execute(
            "UPDATE auth_accounts SET password_hash=? WHERE login=?",
            (hash_password(password), login),
        )
    # Завершить старые сеансы: все войдут заново с новым паролем.
    con.execute("DELETE FROM auth_sessions")
    con.execute(
        "INSERT INTO settings(key,value) VALUES('auth_password_must_change','false') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    con.commit()
    print("Пароль установлен для аккаунтов: " + ", ".join(rows))
    print("Активные сеансы завершены — войдите заново с новым паролем.")
finally:
    con.close()
PY
