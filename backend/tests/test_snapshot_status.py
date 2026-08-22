"""The cameras API reports snapshot freshness, and diagnostics distinguishes
fresh / stale / no-frame so the UI can explain why a card is blank."""
import base64

from app.main import app
from fastapi.testclient import TestClient


def _fake_jpeg() -> str:
    """Return a base64 frame the API accepts (JPEG markers, no PIL needed)."""
    return base64.b64encode(b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9").decode()


def _add_camera(c: TestClient, name: str = "CamA") -> str:
    r = c.post("/api/cameras", json={
        "name": name, "zone": "Z", "description": "", "rtsp_url": "rtsp://u:p@host/stream",
        "fps_limit": 8, "enabled": True,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_camera_reports_no_snapshot_before_any_frame():
    with TestClient(app) as c:
        cid = _add_camera(c)
        item = next(x for x in c.get("/api/cameras").json() if x["id"] == cid)
        assert item.get("snapshot_age_seconds") is None


def test_camera_reports_snapshot_age_after_frame():
    with TestClient(app) as c:
        cid = _add_camera(c, "CamB")
        up = c.post(f"/api/cameras/{cid}/snapshot", json={
            "jpeg_base64": _fake_jpeg(), "captured_at": "2026-01-01T00:00:00+00:00",
        })
        assert up.status_code == 204, up.text

        item = next(x for x in c.get("/api/cameras").json() if x["id"] == cid)
        age = item["snapshot_age_seconds"]
        assert age is not None and 0 <= age <= 5

        diag = c.get("/api/diagnostics").json()
        cam = next(x for x in diag["cameras"] if x["camera_id"] == cid)
        assert cam["snapshot"] == "fresh"


def test_snapshot_missing_returns_404_and_diagnostics_none():
    with TestClient(app) as c:
        cid = _add_camera(c, "CamC")
        assert c.get(f"/api/cameras/{cid}/snapshot").status_code == 404
        diag = c.get("/api/diagnostics").json()
        cam = next(x for x in diag["cameras"] if x["camera_id"] == cid)
        assert cam["snapshot"] == "none"
