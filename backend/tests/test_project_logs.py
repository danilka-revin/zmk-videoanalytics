"""Единая вкладка «Логи»: журнал всего проекта для быстрой диагностики багов.

Проверяется, что в один поток собираются записи из SQLite (подсистемы API),
строки самого процесса API и зеркало stdout/stderr отдельных компонентов,
а фильтры, поиск и CSV-выгрузка отдают ровно выбранный срез.
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from app import main
from fastapi.testclient import TestClient


def _worker_headers() -> dict[str, str]:
    return {"X-Worker-Token": main.WORKER_TOKEN}


def _service_headers() -> dict[str, str]:
    return {"X-Bot-Service-Token": main.BOT_API_TOKEN}


def _insert(level: str, service: str, message: str, camera_id: str | None = None, when: str | None = None) -> None:
    con = main.db()
    con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)", (when or main.now_iso(), level, service, message, camera_id))
    con.commit(); con.close()


def test_project_logs_merges_database_rows_and_api_runtime_lines():
    with TestClient(main.app) as c:
        _insert("ERROR", "camera_manager", "Камера cam_01 потеряла RTSP поток", "cam_01")
        body = c.get("/api/logs/project?hours=24").json()
        assert body["counts"]["ERROR"] >= 1
        assert any(item["message"] == "Камера cam_01 потеряла RTSP поток" and item["source"] == "db" for item in body["items"])
        # Строки самого процесса API (uvicorn, необработанные ошибки) тоже видны.
        assert any(item["source"] == "runtime" and item["service"] == "api" for item in body["items"])
        sources = {item["id"]: item for item in body["sources"]}
        assert sources["camera_manager"]["label"] == "API · камеры" and sources["camera_manager"]["errors"] >= 1
        # Молчащие компоненты остаются в списке: молчание worker-а тоже симптом.
        assert {"inference", "training", "bot-telegram", "bot-max", "updater"} <= set(sources)
        assert sources["updater"]["entries"] == 0 and sources["updater"]["last_timestamp"] is None


def test_service_log_shipping_reaches_the_project_log():
    main._service_log_buckets.clear()
    shipped = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with TestClient(main.app) as c:
        r = c.post("/api/service-logs", headers=_worker_headers(), json={"service": "inference", "entries": [
            {"timestamp": shipped, "level": "warn", "message": "camera cam_01 RTSP reconnect", "camera_id": "cam_01"},
            {"message": "active model loaded"},
        ]})
        assert r.status_code == 202, r.text
        assert r.json() == {"service": "inference", "accepted": 2, "dropped": 0}
        body = c.get("/api/logs/project?service=inference&hours=24").json()
        item = next(entry for entry in body["items"] if entry["message"] == "camera cam_01 RTSP reconnect")
        assert item["level"] == "WARNING" and item["camera_id"] == "cam_01"
        assert item["label"] == "Inference worker" and item["source"] == "db"
        # Время от worker-а приходит в UTC, в журнале хранится часовой пояс площадки.
        assert item["timestamp"].endswith("+07:00") and item["timestamp"] != shipped
        source = next(entry for entry in body["sources"] if entry["id"] == "inference")
        assert source["entries"] == 2 and source["component"] == "inference" and source["age_seconds"] is not None
    main._service_log_buckets.clear()


def test_service_log_shipping_normalizes_levels_and_rejects_bad_input():
    main._service_log_buckets.clear()
    with TestClient(main.app) as c:
        assert c.post("/api/service-logs", headers=_worker_headers(), json={"service": "attacker", "entries": [{"message": "x"}]}).status_code == 422
        assert c.post("/api/service-logs", headers=_worker_headers(), json={"service": "inference", "entries": []}).status_code == 422
        assert c.post("/api/service-logs", headers=_worker_headers(), json={"service": "inference", "entries": [{"message": "x" * 5000}]}).status_code == 422
        r = c.post("/api/service-logs", headers=_worker_headers(), json={"service": "inference", "entries": [
            {"level": "fatal", "message": "segfault in decoder", "camera_id": "../../etc/passwd"},
            {"level": "unknown-level", "message": "  строка с мусором\u0007 "},
        ]})
        assert r.status_code == 202 and r.json()["accepted"] == 2
        items = {entry["message"]: entry for entry in c.get("/api/logs/project?service=inference&hours=24").json()["items"]}
        assert items["segfault in decoder"]["level"] == "CRITICAL"
        assert items["segfault in decoder"]["camera_id"] is None  # небезопасный id камеры отброшен
        assert items["строка с мусором"]["level"] == "INFO"
    main._service_log_buckets.clear()


def test_service_log_shipping_accepts_bot_service_token_and_needs_credentials():
    main._service_log_buckets.clear()
    previous = main.API_KEY
    main.API_KEY = "protected-api"
    try:
        with TestClient(main.app) as c:
            payload = {"service": "bot-telegram", "entries": [{"level": "error", "message": "Telegram polling session stopped"}]}
            assert c.post("/api/service-logs", json=payload).status_code == 401
            assert c.post("/api/service-logs", headers={"X-Worker-Token": "wrong"}, json=payload).status_code == 401
            assert c.post("/api/service-logs", headers=_service_headers(), json=payload).status_code == 202
            body = c.get("/api/logs/project?service=bot-telegram&hours=24", headers={"X-API-Key": "protected-api"}).json()
            assert body["counts"]["ERROR"] == 1 and body["items"][0]["label"] == "Telegram бот"
    finally:
        main.API_KEY = previous
        main._service_log_buckets.clear()


def test_service_log_shipping_is_rate_limited_per_component(monkeypatch):
    main._service_log_buckets.clear()
    monkeypatch.setattr(main, "SERVICE_LOG_RATE_PER_MINUTE", 5)
    with TestClient(main.app) as c:
        payload = {"service": "training", "entries": [{"message": f"строка обучения {i}"} for i in range(20)]}
        r = c.post("/api/service-logs", headers=_service_headers(), json=payload)
        assert r.status_code == 202
        assert r.json() == {"service": "training", "accepted": 5, "dropped": 15}
    main._service_log_buckets.clear()


def test_project_log_filters_search_and_limit():
    with TestClient(main.app) as c:
        _insert("ERROR", "inference", "CUDA out of memory")
        _insert("WARNING", "bot-telegram", "Telegram API недоступен")
        _insert("INFO", "model_manager", "Модель загружена")
        errors = c.get("/api/logs/project?level=ERROR&hours=24").json()
        assert errors["items"] and all(item["level"] == "ERROR" for item in errors["items"])
        assert any("CUDA out of memory" in item["message"] for item in errors["items"])
        search = c.get("/api/logs/project?q=недоступен&hours=24").json()
        assert search["matched"] >= 1 and all("недоступен" in item["message"].casefold() for item in search["items"])
        # Поиск регистронезависимый и для кириллицы (SQLite LIKE так не умеет).
        assert c.get("/api/logs/project?q=НЕДОСТУПЕН&hours=24").json()["matched"] == search["matched"]
        by_service = c.get("/api/logs/project?service=bot-telegram,inference&hours=24").json()
        assert {item["service"] for item in by_service["items"]} <= {"bot-telegram", "inference"}
        assert len(c.get("/api/logs/project?limit=1&hours=24").json()["items"]) == 1
        assert c.get("/api/logs/project?level=NOPE").status_code == 422


def test_project_log_camera_filter_and_period():
    with TestClient(main.app) as c:
        _insert("ERROR", "camera_manager", "Кадр не получен", "cam_03")
        _insert("ERROR", "camera_manager", "Другая камера", "cam_04")
        old = (datetime.now(main.TZ) - timedelta(days=3)).isoformat(timespec="seconds")
        _insert("ERROR", "camera_manager", "Старая запись вне периода", "cam_03", when=old)
        items = c.get("/api/logs/project?camera_id=cam_03&hours=24").json()["items"]
        assert [item["message"] for item in items] == ["Кадр не получен"]
        assert c.get("/api/logs/project?camera_id=cam_03&hours=168").json()["matched"] == 2


def test_project_logs_csv_export_returns_the_same_slice():
    with TestClient(main.app) as c:
        _insert("ERROR", "inference", "CUDA out of memory")
        r = c.get("/api/logs/project.csv?level=ERROR&hours=24")
        assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
        assert "zmk-project-logs-24h.csv" in r.headers["content-disposition"]
        lines = r.text.splitlines()
        assert lines[0] == "timestamp,level,service,source,camera_id,message"
        assert any("CUDA out of memory" in line for line in lines)
        assert all(",ERROR," in line for line in lines[1:])


def test_failed_requests_reach_the_project_log():
    with TestClient(main.app) as c:
        marker = f"/api/missing-{uuid.uuid4().hex[:10]}"
        assert c.get(marker).status_code == 404
        items = c.get("/api/logs/project?hours=1").json()["items"]
        hit = next(item for item in items if marker in item["message"])
        assert hit["level"] == "WARNING" and hit["source"] == "runtime" and hit["service"] == "api"


def test_dashboard_reports_error_badge_for_the_logs_tab():
    with TestClient(main.app) as c:
        _insert("CRITICAL", "inference", "worker упал")
        assert c.get("/api/dashboard").json()["log_errors_24h"] >= 1


def test_project_logs_stay_admin_only_for_telegram_roles():
    old_key, old_token, old_roles = main.API_KEY, main.TELEGRAM_BOT_TOKEN, main.TELEGRAM_ROLES
    main.API_KEY = "protected-api"; main.TELEGRAM_BOT_TOKEN = "123456:test-token"; main.TELEGRAM_ROLES = {100: "admin", 200: "operator"}
    try:
        with TestClient(main.app) as c:
            admin = {"X-Telegram-Init-Data": _telegram_init_data(main.TELEGRAM_BOT_TOKEN, 100)}
            operator = {"X-Telegram-Init-Data": _telegram_init_data(main.TELEGRAM_BOT_TOKEN, 200)}
            assert c.get("/api/logs/project", headers=admin).status_code == 200
            assert c.get("/api/logs/project", headers=operator).status_code == 403
            assert c.get("/api/logs/project.csv", headers=operator).status_code == 403
    finally:
        main.API_KEY, main.TELEGRAM_BOT_TOKEN, main.TELEGRAM_ROLES = old_key, old_token, old_roles


def test_worker_timestamps_are_normalized_to_the_site_timezone():
    naive = "2026-01-02T03:04:05"
    assert main.normalize_log_timestamp(naive) == "2026-01-02T10:04:05+07:00"
    assert main.normalize_log_timestamp("2026-01-02T03:04:05Z") == "2026-01-02T10:04:05+07:00"
    assert main.normalize_log_timestamp("не дата").endswith("+07:00")
    assert main.clean_log_message("  строка\u0007с\x0bмусором  ") == "строка с мусором"


def _telegram_init_data(token: str, user_id: int) -> str:
    values = {"auth_date": str(int(time.time())), "query_id": "logs-query", "user": json.dumps({"id": user_id}, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_project_log_accepts_a_list_of_levels():
    with TestClient(main.app) as c:
        _insert("ERROR", "inference", "CUDA error")
        _insert("CRITICAL", "api", "Необработанное исключение")
        _insert("INFO", "api", "Обычная строка")
        body = c.get("/api/logs/project?level=ERROR,CRITICAL&hours=24").json()
        assert body["filters"]["level"] == "CRITICAL,ERROR"
        assert {item["level"] for item in body["items"]} <= {"ERROR", "CRITICAL"}
        assert body["matched"] >= 2
        assert c.get("/api/logs/project?level=ERROR,NOPE").status_code == 422


def test_shipped_logs_cannot_grow_the_journal_forever(monkeypatch):
    """Потолок по числу строк: зеркало stdout не должно раздувать базу."""
    main._service_log_buckets.clear()
    main._logs_prune_at = 0.0
    monkeypatch.setattr(main, "LOG_TABLE_MAX_ROWS", 10)
    with TestClient(main.app) as c:
        for index in range(25):
            _insert("INFO", "camera_manager", f"старая запись {index}")
        r = c.post("/api/service-logs", headers=_worker_headers(), json={"service": "inference", "entries": [{"message": "новая строка worker-а"}]})
        assert r.status_code == 202 and r.json()["accepted"] == 1
        con = main.db(); total = con.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        newest = con.execute("SELECT message FROM logs ORDER BY id DESC LIMIT 1").fetchone()[0]
        con.close()
        assert total <= 10 and newest == "новая строка worker-а"
    main._service_log_buckets.clear()
    main._logs_prune_at = 0.0
