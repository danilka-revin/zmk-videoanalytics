"""Camera runtime contract: worker heartbeat, secure diagnostics and restart."""
import base64

from app import main
from fastapi.testclient import TestClient


def _camera(client: TestClient) -> str:
    response = client.post(
        "/api/cameras",
        json={
            "name": "Runtime Cam",
            "zone": "Test",
            "rtsp_url": "rtsp://user:password@camera.internal:554/stream",
            "fps_limit": 8,
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_camera_schema_migrates_existing_database(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.db"
    con = main.sqlite3.connect(legacy)
    con.execute("CREATE TABLE cameras(id TEXT PRIMARY KEY, name TEXT NOT NULL, zone TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', rtsp_url TEXT NOT NULL DEFAULT '', fps_limit REAL NOT NULL DEFAULT 8, status TEXT NOT NULL DEFAULT 'unknown', fps REAL NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)")
    con.commit(); con.close()
    monkeypatch.setattr(main, "DB_PATH", legacy)
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    main.init_db()
    con = main.sqlite3.connect(legacy)
    columns = {row[1] for row in con.execute("PRAGMA table_info(cameras)")}
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"telemetry_at", "last_error", "restart_requested_at"} <= columns
    assert "worker_status" in tables


def test_camera_accepts_connecting_telemetry_and_redacts_url(monkeypatch):
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    with TestClient(main.app) as client:
        camera_id = _camera(client)
        response = client.post(
            f"/api/cameras/{camera_id}/telemetry",
            json={
                "status": "connecting",
                "fps": 0,
                "latency_ms": 12,
                "error": "open failed rtsp://user:password@camera.internal:554/stream",
            },
        )
        assert response.status_code == 200, response.text
        camera = client.get(f"/api/cameras/{camera_id}").json()
        assert camera["status"] == "connecting"
        assert "password" not in camera["last_error"]
        assert "<rtsp-url>" in camera["last_error"]


def test_restart_requests_new_worker_session_and_clears_snapshot(monkeypatch):
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    with TestClient(main.app) as client:
        camera_id = _camera(client)
        jpeg = base64.b64encode(b"\xff\xd8" + b"frame" * 8 + b"\xff\xd9").decode()
        assert client.post(f"/api/cameras/{camera_id}/snapshot", json={"jpeg_base64": jpeg}).status_code == 204
        restarted = client.post(f"/api/cameras/{camera_id}/restart")
        assert restarted.status_code == 200, restarted.text
        assert restarted.json()["status"] == "connecting"
        camera = client.get(f"/api/cameras/{camera_id}").json()
        assert camera["status"] == "connecting"
        assert camera["restart_requested_at"]
        assert client.get(f"/api/cameras/{camera_id}/snapshot").status_code == 404


def test_internal_heartbeat_exposes_worker_liveness(monkeypatch):
    monkeypatch.setattr(main, "WORKER_TOKEN", "worker-test-token")
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    with TestClient(main.app) as client:
        denied = client.post("/api/internal/inference/heartbeat", json={"status": "running"})
        assert denied.status_code == 401
        accepted = client.post(
            "/api/internal/inference/heartbeat",
            headers={"X-Worker-Token": "worker-test-token"},
            json={"status": "running", "detail": "cameras=1 model=none", "camera_count": 1},
        )
        assert accepted.status_code == 204
        diagnostics = client.get("/api/diagnostics").json()
        assert diagnostics["worker"]["connected"] is True
        assert diagnostics["worker"]["camera_count"] == 1
        assert diagnostics["system"]["worker"]["status"] == "running"
