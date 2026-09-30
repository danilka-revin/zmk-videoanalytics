import json

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


def test_recorded_build_info_drives_the_displayed_version(tmp_path, monkeypatch):
    """Commit-based updates: the panel shows "<VERSION>+<short commit>".

    The installers and the updater service record every applied build in
    data/build-info.json; the API reads it next to the SQLite database instead
    of relying on a hardcoded version constant.
    """
    from app import main

    path = tmp_path / "build-info.json"
    path.write_text(json.dumps({
        "version": "2.23.0",
        "commit": "a" * 40,
        "branch": "main",
        "channel": "commit",
        "installed_at": "2026-01-01T00:00:00Z",
    }))
    monkeypatch.setenv("ZMK_BUILD_INFO", str(path))
    info = main._read_build_info()
    assert info["version"] == "2.23.0"
    assert info["short"] == "a" * 7
    assert info["branch"] == "main"
    assert main._display_version(info) == "2.23.0+" + "a" * 7


def test_missing_build_info_falls_back_to_the_base_version(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setenv("ZMK_BUILD_INFO", str(tmp_path / "absent.json"))
    info = main._read_build_info()
    assert info["commit"] == ""
    assert main._display_version(info) == main.BASE_VERSION


def test_update_status_fallback_reports_commit_fields():
    from app import main

    state = main._local_update_state("updater недоступна")
    for key in ["current_commit", "latest_commit", "commits_behind", "channel", "branch"]:
        assert key in state
    assert state["available"] is False
