"""Independent person/PPE/behaviour model slots form one validated pipeline."""
from __future__ import annotations

from app import main
from fastapi.testclient import TestClient


def test_specialised_model_slot_is_exposed_to_worker_and_event_gateway(monkeypatch):
    monkeypatch.setattr(main, "WORKER_TOKEN", "pipeline-worker-token")
    headers = {"X-Worker-Token": "pipeline-worker-token"}
    with TestClient(main.app) as client:
        models = client.get("/api/models").json()
        primary = next(item["name"] for item in models if item["active"])
        helmet_model = next(item["name"] for item in models if item["name"] != primary and item["status"] == "ready")

        assigned = client.post(f"/api/models/{helmet_model}/activate-slot", json={"role": "helmet"})
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["slots"] == {"helmet": helmet_model}
        pipeline = client.get("/api/models/pipeline").json()
        helmet = next(item for item in pipeline["roles"] if item["id"] == "helmet")
        assert helmet["model"] == helmet_model and helmet["ready"] is True

        internal = client.get("/api/internal/active-models", headers=headers)
        assert internal.status_code == 200, internal.text
        assert internal.json()["primary"]["name"] == primary
        assert internal.json()["slots"] == [{"role": "helmet", "name": helmet_model, "format": "ONNX FP32", "artifact_uri": "file:///test/v20.onnx", "checksum": "", "source": "fixture", "test_mode": False}]

        accepted = client.post("/api/inference/detections", json={"detections": [{
            "camera_id": "cam_01", "model_name": helmet_model, "event_type": "no_helmet", "confidence": .99,
            "person_id": "SLOT-HELMET-1", "detection_id": "slot-helmet-1",
        }]})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["active_models"] == sorted([primary, helmet_model])
        assert accepted.json()["accepted"]

        listed = client.get("/api/models").json()
        slot_item = next(item for item in listed if item["name"] == helmet_model)
        assert slot_item["slot_roles"] == ["helmet"] and slot_item["pipeline_active"] is True

        cleared = client.delete("/api/models/pipeline/helmet")
        assert cleared.status_code == 200 and cleared.json()["cleared"] is True
        rejected = client.post("/api/inference/detections", json={"detections": [{
            "camera_id": "cam_01", "model_name": helmet_model, "event_type": "no_helmet", "confidence": .99,
            "person_id": "SLOT-HELMET-2", "detection_id": "slot-helmet-2",
        }]})
        assert rejected.status_code == 200
        assert any("stale_model" in item["reason"] for item in rejected.json()["rejected"])
