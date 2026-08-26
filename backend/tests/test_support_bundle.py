"""A support bundle must remain useful without exporting deployment secrets."""
from __future__ import annotations

import io
import json
import zipfile

from app import main
from fastapi.testclient import TestClient


def test_support_bundle_contains_safe_operational_state(monkeypatch):
    monkeypatch.setattr(main, "system_health_data", lambda: {"cpu": 5, "ram": 10, "disk": 20, "worker": {"connected": True}})
    monkeypatch.setattr(main, "cameras", lambda: [{"id": "cam_safe", "name": "Safe camera", "configured": True}])
    monkeypatch.setattr(main, "build_overview_analytics", lambda hours: {"hours": hours, "totals": {"events": 2}})
    monkeypatch.setattr(main, "error_report", lambda hours: {"period_hours": hours, "generated_at": main.now_iso(), "summary": {"ERROR": 0}, "items": []})
    with TestClient(main.app) as client:
        response = client.get("/api/reports/support.zip?hours=24")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert {"README.txt", "system-health.json", "camera-status.json", "analytics.json", "error-summary.json"} <= set(bundle.namelist())
        assert json.loads(bundle.read("analytics.json"))["hours"] == 24
        readme = bundle.read("README.txt").decode("utf-8")
        assert "RTSP URL" in readme and "токены" in readme


def test_support_bundle_never_serializes_rtsp_urls():
    with TestClient(main.app) as client:
        response = client.get("/api/reports/support.zip?hours=24")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        camera_state = bundle.read("camera-status.json").decode("utf-8")
        assert "rtsp://" not in camera_state and "rtsps://" not in camera_state
