"""Тестовые прогоны моделей: PPE-тест и ролик вместо камеры.

Оператор должен увидеть нарушение в журнале («человек без каски»), даже
когда работает только тестовый режим. Такие события помечаются `is_test`,
поэтому клипы и trial-модели никогда не превращаются в production-оповещения
(webhook, боты) и легко отличаются в журнале и отчётах.
"""
from app import main
from fastapi.testclient import TestClient


def active_model(client: TestClient) -> str:
    return next(item["name"] for item in client.get("/api/models").json() if item["active"])


def _no_helmet_payload(model: str, *, confidence: float, person: str, test: bool, camera: str = "cam_01") -> dict:
    detection = {
        "camera_id": camera, "model_name": model, "event_type": "no_helmet",
        "confidence": confidence, "person_id": person, "bbox": [10, 20, 100, 200],
    }
    if test:
        detection["test_mode"] = True
    return {"detections": [detection]}


def _enable_webhook() -> None:
    con = main.db()
    for key, value in (("webhook_enabled", "true"), ("webhook_url", "https://skud.internal/events")):
        con.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit(); con.close()


class _WebhookResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None


def test_ppe_trial_detection_is_recorded_as_a_test_event_without_webhook(monkeypatch):
    calls: list = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _WebhookResponse()

    monkeypatch.setattr(main.httpx, "post", fake_post)
    with TestClient(main.app) as c:
        _enable_webhook()
        model = active_model(c)
        # В тестовом режиме worker показывает рамки от model_test_conf (.10),
        # поэтому 0.4 — валидная детекция для проверки, но ниже production
        # порога «каска» (0.85).
        test_confidence = .4
        assert main.MODEL_TEST_CONF_DEFAULT < test_confidence
        result = c.post("/api/inference/detections", json=_no_helmet_payload(model, confidence=test_confidence, person="PPE-TEST-1", test=True))
        assert result.status_code == 200, result.text
        body = result.json()
        assert len(body["accepted"]) == 1, body
        assert body["accepted"][0]["test"] is True
        assert calls == []

        events = [event for event in c.get("/api/events?limit=100").json() if event["person_id"] == "PPE-TEST-1"]
        assert len(events) == 1
        assert events[0]["is_test"] == 1
        assert events[0]["type"] == "no_helmet"

        # Production-детекция по-прежнему уходит во внешнюю интеграцию.
        production = c.post("/api/inference/detections", json=_no_helmet_payload(model, confidence=0.97, person="PROD-HOOK-1", test=False))
        assert production.status_code == 200 and len(production.json()["accepted"]) == 1
        assert [url for url, _ in calls] == ["https://skud.internal/events"]


def test_production_detection_stays_non_test_and_keeps_the_strict_threshold():
    with TestClient(main.app) as c:
        model = active_model(c)
        result = c.post("/api/inference/detections", json=_no_helmet_payload(model, confidence=0.4, person="PROD-1", test=False))
        assert result.status_code == 200
        body = result.json()
        # Ниже production-порога — событие не создаётся.
        assert body["accepted"] == [] and any("below_threshold" in item["reason"] for item in body["rejected"])

        accepted = c.post("/api/inference/detections", json=_no_helmet_payload(model, confidence=0.97, person="PROD-2", test=False))
        assert accepted.status_code == 200 and len(accepted.json()["accepted"]) == 1
        event = next(item for item in c.get("/api/events?limit=100").json() if item["person_id"] == "PROD-2")
        assert event["is_test"] == 0


def test_uploaded_video_events_are_marked_as_test(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "TEST_VIDEO_DIR", tmp_path / "videos")
    monkeypatch.setattr(main, "sync_go2rtc_cameras", lambda: {"ok": True})
    with TestClient(main.app) as c:
        uploaded = c.post("/api/test-videos?filename=shift.mp4", content=b"0" * 2048, headers={"Content-Type": "video/mp4"})
        assert uploaded.status_code == 201, uploaded.text
        camera_id = uploaded.json()["id"]
        # В реальном контуре worker сначала публикует телеметрию первого кадра.
        telemetry = c.post(f"/api/cameras/{camera_id}/telemetry", json={"status": "online", "fps": 8, "latency_ms": 120, "error": ""})
        assert telemetry.status_code == 200, telemetry.text
        model = active_model(c)
        result = c.post("/api/inference/detections", json=_no_helmet_payload(model, confidence=.96, person="VIDEO-1", test=False, camera=camera_id))
        assert result.status_code == 200, result.text
        assert len(result.json()["accepted"]) == 1
        events = [event for event in c.get("/api/events?limit=100").json() if event["person_id"] == "VIDEO-1"]
        assert len(events) == 1 and events[0]["is_test"] == 1
        c.delete(f"/api/cameras/{camera_id}?delete_events=true")


def test_event_journal_clear_needs_explicit_confirmation_and_removes_frames():
    with TestClient(main.app) as c:
        events = c.get("/api/events?limit=5").json()
        assert events
        fixture_size = len(main.rows("SELECT id FROM events"))
        evidence = main.event_frame_path_for(int(events[0]["id"]))
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_bytes(b"\xff\xd8frame\xff\xd9")

        # Одно нажатие без подтверждения ничего не удаляет.
        assert c.post("/api/events/clear", json={"confirm": False}).status_code == 422
        assert c.get("/api/events?limit=5").json()

        cleared = c.post("/api/events/clear", json={"confirm": True})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["cleared"] == fixture_size  # весь журнал, а не только видимые 5
        assert c.get("/api/events?limit=5").json() == []
        assert not evidence.exists()
        log = main.rows("SELECT message FROM logs WHERE service='event_manager' ORDER BY id DESC LIMIT 1")
        assert log and "cleared" in log[0]["message"]


def test_event_clear_is_admin_only_and_test_events_reach_the_export():
    assert main._role_request_allowed("admin", "POST", "/api/events/clear") is True
    assert main._role_request_allowed("analyst", "POST", "/api/events/clear") is False
    assert main._role_request_allowed("viewer", "POST", "/api/events/clear") is False
    with TestClient(main.app) as c:
        con = main.db()
        con.execute(
            "INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,is_test) VALUES(?,?,?,?,?,?,?)",
            (main.now_iso(), "cam_01", "no_helmet", "high", .93, "EXPORT-TEST", 1),
        )
        con.commit(); con.close()
        response = c.get("/api/reports/events.csv?q=EXPORT-TEST")
        assert response.status_code == 200
        text = response.content.decode("utf-8-sig")
        header = text.splitlines()[0]
        assert "Тестовое событие" in header
        assert text.splitlines()[1].split(";")[header.split(";").index("Тестовое событие")] == "Да"
