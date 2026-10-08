"""События тестового видео остаются в журнале и удаляются отдельно.

Ролик, загруженный вместо камеры («Проверить по видео»), пишет найденные
нарушения в журнал с отметкой ТЕСТ. Остановка теста убирает только сам
источник и файл — события остаются, пока администратор не удалит их выбором в
«События» (`POST /api/events/delete-bulk`) или полной очисткой журнала.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from app import main
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_video_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "TEST_VIDEO_DIR", tmp_path / "uploaded-test-videos")
    monkeypatch.setattr(main, "sync_go2rtc_cameras", lambda: {"ok": True})


def _active_model(client: TestClient) -> str:
    return next(item["name"] for item in client.get("/api/models").json() if item["active"])


def _start_video_test(client: TestClient, filename: str = "shift.mp4") -> str:
    uploaded = client.post(f"/api/test-videos?filename={filename}", content=b"0" * 2048, headers={"Content-Type": "video/mp4"})
    assert uploaded.status_code == 201, uploaded.text
    camera_id = uploaded.json()["id"]
    # В реальном контуре worker сначала публикует телеметрию первого кадра.
    telemetry = client.post(f"/api/cameras/{camera_id}/telemetry", json={"status": "online", "fps": 8, "latency_ms": 120, "error": ""})
    assert telemetry.status_code == 200, telemetry.text
    return camera_id


def _detect(client: TestClient, camera_id: str, person: str) -> int:
    result = client.post("/api/inference/detections", json={"detections": [{
        "camera_id": camera_id, "model_name": _active_model(client), "event_type": "no_helmet",
        "confidence": .96, "person_id": person, "bbox": [10, 20, 100, 200],
    }]})
    assert result.status_code == 200, result.text
    accepted = result.json()["accepted"]
    assert len(accepted) == 1, result.json()
    return int(accepted[0]["event_id"])


def _store_frame(event_id: int):
    path = main.event_frame_path_for(event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8evidence\xff\xd9")
    return path


def _event_ids(client: TestClient) -> set[int]:
    return {int(item["id"]) for item in client.get("/api/events?limit=500").json()}


def test_stopping_a_video_test_keeps_its_events_with_name_frames_and_review():
    with TestClient(main.app) as client:
        camera_id = _start_video_test(client)
        first, second = _detect(client, camera_id, "VIDEO-A"), _detect(client, camera_id, "VIDEO-B")
        frames = [_store_frame(first), _store_frame(second)]
        clip = next(main.TEST_VIDEO_DIR.glob("*.mp4"))

        stopped = client.delete(f"/api/cameras/{camera_id}")  # как «Остановить тест» в панели
        assert stopped.status_code == 200, stopped.text
        assert stopped.json() == {"id": camera_id, "deleted": True, "deleted_events": 0, "kept_events": 2}

        # Источник и файл ролика убраны…
        assert not clip.exists()
        assert camera_id not in {item["id"] for item in client.get("/api/cameras").json()}
        assert all(item["id"] != camera_id for item in main.internal_cameras())
        # …а события остались: читаемое имя, зона, отметка ТЕСТ и кадр-доказательство.
        kept = {item["id"]: item for item in client.get("/api/events?limit=500").json() if item["camera_id"] == camera_id}
        assert set(kept) == {first, second}
        for event in kept.values():
            assert event["camera_name"] == "Тест · shift" and event["zone"] == "Тест по видео"
            assert event["is_test"] == 1 and event["has_frame"] is True
        single = client.get(f"/api/events/by-id/{first}")
        assert single.status_code == 200 and single.json()["camera_name"] == "Тест · shift"
        assert client.get(f"/api/events/{first}/frame").status_code == 200
        assert all(path.exists() for path in frames)

        # Событие без камеры можно проверять как обычно (FK камеры не мешает).
        assert client.post(f"/api/events/{first}/ack", json={"note": "проверено"}).status_code == 200
        assert client.post("/api/events/reject-bulk", json={"event_ids": [second]}).status_code == 200

        # Поиск, отчёт и аналитика продолжают видеть такие события.
        found = client.get("/api/search?q=shift&kinds=event").json()["results"]
        assert {item["id"] for item in found} >= {first, second}
        report = client.get("/api/reports/events.csv?q=shift").content.decode("utf-8-sig")
        assert report.count("Тест · shift") == 2 and "Тест по видео" in report
        cameras = client.get("/api/analytics/overview?hours=24").json()["cameras"]
        assert any(item["camera_id"] == camera_id and item["name"] == "Тест · shift" and item["total"] == 2 for item in cameras)

        log = main.rows("SELECT message FROM logs WHERE service='camera_manager' AND camera_id=? ORDER BY id DESC LIMIT 1", (camera_id,))
        assert "events kept: 2" in log[0]["message"]


def test_kept_video_events_survive_restart_and_stay_deletable(monkeypatch):
    with TestClient(main.app) as client:
        camera_id = _start_video_test(client)
        event_id = _detect(client, camera_id, "VIDEO-RESTART")
        assert client.delete(f"/api/cameras/{camera_id}").status_code == 200
    # Перезапуск API (init_db, миграции) не должен терять события без камеры.
    # Фикстуры тестовой БД повторно не заливаем: в продакшене их нет.
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    with TestClient(main.app) as client:
        assert event_id in _event_ids(client)
        removed = client.post("/api/events/delete-bulk", json={"event_ids": [event_id]})
        assert removed.status_code == 200 and removed.json()["deleted_ids"] == [event_id]
        assert event_id not in _event_ids(client)


def test_video_test_without_events_stops_cleanly():
    with TestClient(main.app) as client:
        camera_id = _start_video_test(client)
        stopped = client.delete(f"/api/cameras/{camera_id}")
        assert stopped.status_code == 200
        assert stopped.json() == {"id": camera_id, "deleted": True, "deleted_events": 0, "kept_events": 0}


def test_video_test_linked_to_a_dataset_job_is_not_orphaned_when_events_are_kept():
    """Проверка внешних ключей отключается только ради событий, не ради заданий."""
    with TestClient(main.app) as client:
        camera_id = _start_video_test(client)
        event_id = _detect(client, camera_id, "VIDEO-JOB")
        con = main.db()
        con.execute("INSERT INTO dataset_capture_jobs(name,camera_id,target_count,capture_fps,status,captured_count,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("ds-from-video", camera_id, 10, 1.0, "done", 10, "Готово", main.now_iso(), main.now_iso()))
        con.commit(); con.close()

        refused = client.delete(f"/api/cameras/{camera_id}")
        assert refused.status_code == 409 and "задания" in refused.json()["detail"]
        # Ничего не изменилось: источник на месте, событие не помечено и не потеряно.
        assert camera_id in {item["id"] for item in client.get("/api/cameras").json()}
        assert event_id in _event_ids(client)
        stored = main.rows("SELECT camera_label,zone_label FROM events WHERE id=?", (event_id,))
        assert stored == [{"camera_label": "", "zone_label": ""}]
        assert main.rows("SELECT COUNT(*) n FROM dataset_capture_jobs WHERE camera_id=?", (camera_id,))[0]["n"] == 1

        # После удаления задания остановка теста проходит как обычно.
        con = main.db()
        con.execute("DELETE FROM dataset_capture_jobs WHERE camera_id=?", (camera_id,))
        con.commit(); con.close()
        stopped = client.delete(f"/api/cameras/{camera_id}")
        assert stopped.status_code == 200 and stopped.json()["kept_events"] == 1
        assert event_id in _event_ids(client)


def test_explicit_delete_events_still_removes_video_test_events_and_frames():
    with TestClient(main.app) as client:
        camera_id = _start_video_test(client)
        event_id = _detect(client, camera_id, "VIDEO-PURGE")
        frame = _store_frame(event_id)
        removed = client.delete(f"/api/cameras/{camera_id}?delete_events=true")
        assert removed.status_code == 200, removed.text
        assert removed.json()["deleted_events"] == 1 and removed.json()["kept_events"] == 0
        assert event_id not in _event_ids(client) and not frame.exists()


def test_rtsp_camera_with_events_still_needs_explicit_confirmation():
    with TestClient(main.app) as client:
        assert main.rows("SELECT COUNT(*) n FROM events WHERE camera_id='cam_01'")[0]["n"] > 0
        blocked = client.delete("/api/cameras/cam_01")
        assert blocked.status_code == 409 and "delete_events=true" in blocked.json()["detail"]
        assert client.get("/api/cameras").json()
        assert main.rows("SELECT COUNT(*) n FROM cameras WHERE id='cam_01'")[0]["n"] == 1
        removed = client.delete("/api/cameras/cam_01?delete_events=true")
        assert removed.status_code == 200 and removed.json()["kept_events"] == 0
        assert main.rows("SELECT COUNT(*) n FROM events WHERE camera_id='cam_01'")[0]["n"] == 0


def test_delete_selected_events_removes_only_the_selection_and_its_frames():
    with TestClient(main.app) as client:
        camera_id = _start_video_test(client)
        test_ids = [_detect(client, camera_id, f"VIDEO-SEL-{index}") for index in range(3)]
        frames = {event_id: _store_frame(event_id) for event_id in test_ids}
        # События одной секунды имеют одинаковую метку времени, поэтому номер
        # «обычного» события берём не первым в списке, а любым вне тестовых.
        production_id = next(int(item["id"]) for item in client.get("/api/events?limit=50").json() if int(item["id"]) not in set(test_ids))
        production_frame = _store_frame(production_id)
        before = _event_ids(client)

        selection = [test_ids[0], test_ids[1], test_ids[1], 987654]  # дубль и несуществующий номер
        result = client.post("/api/events/delete-bulk", json={"event_ids": selection})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["deleted"] == 2 and body["deleted_ids"] == sorted(test_ids[:2]) and body["deleted_test"] == 2
        assert body["missing_ids"] == [987654]

        assert _event_ids(client) == before - set(test_ids[:2])
        assert not frames[test_ids[0]].exists() and not frames[test_ids[1]].exists()
        assert frames[test_ids[2]].exists() and production_frame.exists()
        log = main.rows("SELECT level,message FROM logs WHERE service='event_manager' ORDER BY id DESC LIMIT 1")[0]
        assert log["level"] == "WARNING" and "Events deleted: events=2 test=2 missing=1" in log["message"]

        # Повторное удаление тех же номеров ничего не ломает.
        again = client.post("/api/events/delete-bulk", json={"event_ids": test_ids[:2]})
        assert again.status_code == 200 and again.json()["deleted"] == 0 and again.json()["missing_ids"] == sorted(test_ids[:2])


def test_delete_selected_events_can_mix_test_and_production_events():
    with TestClient(main.app) as client:
        camera_id = _start_video_test(client)
        test_id = _detect(client, camera_id, "VIDEO-MIX")
        production_ids = [int(item["id"]) for item in client.get("/api/events?limit=500").json() if not item["is_test"]][:2]
        body = client.post("/api/events/delete-bulk", json={"event_ids": [test_id, *production_ids]}).json()
        assert body["deleted"] == 3 and body["deleted_test"] == 1
        assert not _event_ids(client) & {test_id, *production_ids}


@pytest.mark.parametrize("payload", [{"event_ids": []}, {"event_ids": [0]}, {"event_ids": [-3, 4]}, {"event_ids": list(range(1, 502))}, {}])
def test_delete_selected_events_validates_the_selection(payload):
    with TestClient(main.app) as client:
        before = _event_ids(client)
        assert client.post("/api/events/delete-bulk", json=payload).status_code == 422
        assert _event_ids(client) == before


def test_delete_selected_events_is_admin_only_in_the_panel(monkeypatch):
    assert main._role_request_allowed("admin", "POST", "/api/events/delete-bulk") is True
    assert main._role_request_allowed("analyst", "POST", "/api/events/delete-bulk") is False
    assert main._role_request_allowed("viewer", "POST", "/api/events/delete-bulk") is False

    monkeypatch.setattr(main, "PASSWORD_AUTH_ENABLED", True)
    monkeypatch.setattr(main, "INITIAL_APP_PASSWORD", main.DEFAULT_INITIAL_APP_PASSWORD)
    main._auth_attempts.clear()
    with TestClient(main.app) as admin:
        assert admin.post("/api/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        assert admin.post("/api/auth/accounts", json={"login": "ivanov", "label": "Иванов · аналитик", "role": "analyst", "password": "shift-2026"}).status_code == 200
        event_ids = [int(item["id"]) for item in admin.get("/api/events?limit=3").json()]

        analyst = TestClient(main.app)
        assert analyst.post("/api/auth/login", json={"login": "ivanov", "password": "shift-2026"}).status_code == 200
        denied = analyst.post("/api/events/delete-bulk", json={"event_ids": event_ids})
        assert denied.status_code == 403
        assert _event_ids(analyst) >= set(event_ids)  # аналитик видит события, но ничего не удалил

        removed = admin.post("/api/events/delete-bulk", json={"event_ids": event_ids})
        assert removed.status_code == 200 and removed.json()["deleted"] == 3
        assert removed.json()["deleted_by"] == "admin"


def _telegram_init_data(token: str, user_id: int) -> str:
    values = {"auth_date": str(int(time.time())), "query_id": "test-query", "user": json.dumps({"id": user_id}, separators=(",", ":"))}
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_delete_selected_events_in_telegram_mini_app_is_admin_only(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "protected-api")
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setattr(main, "TELEGRAM_ROLES", {100: "admin", 200: "operator", 300: "viewer"})
    with TestClient(main.app) as client:
        admin, operator, viewer = (
            {"X-Telegram-Init-Data": _telegram_init_data(main.TELEGRAM_BOT_TOKEN, user_id)} for user_id in (100, 200, 300)
        )
        ids = [int(item["id"]) for item in client.get("/api/events?limit=4", headers=admin).json()]
        assert len(ids) == 4
        assert client.post("/api/events/delete-bulk", json={"event_ids": ids[:1]}, headers=viewer).status_code == 403
        assert client.post("/api/events/delete-bulk", json={"event_ids": ids[:1]}, headers=operator).status_code == 403
        # Оператор по-прежнему принимает/отклоняет пакетом — удаление ему недоступно.
        assert client.post("/api/events/ack-bulk", json={"event_ids": ids[:1]}, headers=operator).status_code == 200
        removed = client.post("/api/events/delete-bulk", json={"event_ids": ids[:2]}, headers=admin)
        assert removed.status_code == 200, removed.text
        assert removed.json()["deleted"] == 2 and removed.json()["deleted_by"].startswith("telegram")


def test_event_label_columns_are_added_to_a_legacy_journal(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy-journal.db"
    con = main.sqlite3.connect(legacy)
    con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, camera_id TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, confidence REAL NOT NULL, person_id TEXT, external_id TEXT, acknowledged INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', FOREIGN KEY(camera_id) REFERENCES cameras(id))")
    con.execute("INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id) VALUES(?,?,?,?,?,?)", (main.now_iso(), "cam_old", "no_helmet", "high", .9, "OLD-1"))
    con.commit(); con.close()
    monkeypatch.setattr(main, "DB_PATH", legacy)
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    main.init_db()
    main.init_db()  # повторный запуск (рестарт) безопасен
    con = main.sqlite3.connect(legacy)
    columns = {row[1] for row in con.execute("PRAGMA table_info(events)")}
    kept = con.execute("SELECT person_id,camera_label,zone_label FROM events").fetchone()
    con.close()
    assert {"camera_label", "zone_label"} <= columns
    assert kept == ("OLD-1", "", "")
    # Событие, у которого камеры уже нет и названия не запомнено, не пропадает
    # из журнала: вместо имени показывается номер камеры.
    with TestClient(main.app) as client:
        listed = client.get("/api/events?limit=10").json()
        assert [(item["person_id"], item["camera_name"], item["zone"]) for item in listed] == [("OLD-1", "cam_old", "")]
