"""Model selection must report the inference worker's real load outcome."""
from app import main
from fastapi.testclient import TestClient


def test_active_model_reports_worker_loading_ready_and_error_states(monkeypatch):
    monkeypatch.setattr(main, "WORKER_TOKEN", "runtime-test-token")
    headers = {"X-Worker-Token": "runtime-test-token"}
    with TestClient(main.app) as client:
        active = next(item for item in client.get("/api/models").json() if item["active"])["name"]

        loading = client.post("/api/internal/inference/heartbeat", headers=headers, json={
            "status": "running", "detail": "loading", "camera_count": 1,
            "model_name": active, "model_status": "loading", "model_error": "",
        })
        assert loading.status_code == 204
        item = next(row for row in client.get("/api/models").json() if row["name"] == active)
        assert item["runtime"]["status"] == "loading"
        assert item["runtime"]["worker_connected"] is True

        ready = client.post("/api/internal/inference/heartbeat", headers=headers, json={
            "status": "running", "detail": "loaded", "camera_count": 1,
            "model_name": active, "model_status": "ready", "model_error": "",
        })
        assert ready.status_code == 204
        health = client.get("/api/models/active/health")
        assert health.status_code == 200
        assert health.json()["runtime"]["status"] == "ready"

        failed = client.post("/api/internal/inference/heartbeat", headers=headers, json={
            "status": "running", "detail": "load failed", "camera_count": 1,
            "model_name": active, "model_status": "error", "model_error": "Unsupported model graph",
        })
        assert failed.status_code == 204
        item = next(row for row in client.get("/api/models").json() if row["name"] == active)
        assert item["runtime"]["status"] == "error"
        assert item["runtime"]["detail"] == "Unsupported model graph"
        assert item["runtime"]["worker_connected"] is True
