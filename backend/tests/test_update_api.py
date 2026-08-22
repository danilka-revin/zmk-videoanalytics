from app.main import UPDATE_SERVICE_URL, app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_update_status_degrades_when_updater_unconfigured():
    # Without UPDATE_SERVICE_URL the endpoint must report honestly that the
    # updater service is unavailable (not fake an update).
    if UPDATE_SERVICE_URL:
        return
    r = client.get("/api/update/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["update_available"] is False
    assert "updater" in body["reason"].lower()


def test_update_apply_degrades_when_updater_unconfigured():
    if UPDATE_SERVICE_URL:
        return
    r = client.post("/api/update/apply")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unavailable"
    assert "start.sh" in body["message"] or "start.ps1" in body["message"]
