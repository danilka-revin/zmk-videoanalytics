"""Ready-made model presets: catalog listing and one-click download+register.

The download is exercised against a local mirror server serving a small file
so the test is deterministic and offline; the real public YOLO URLs are
verified separately (see the smoke check in the release notes).
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from app import main
from fastapi.testclient import TestClient


class _Handler(BaseHTTPRequestHandler):
    payload = b"pretended-model-bytes"

    def do_GET(self):
        if self.path.endswith("yolo11n.pt"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


class _MirrorService:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)

    def __enter__(self):
        self.t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *a):
        self.server.shutdown()
        self.t.join(timeout=5)


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    d = tmp_path / "models"
    monkeypatch.setattr(main, "MODEL_DIR", d)
    return d


@pytest.fixture
def preset_url():
    _Handler.payload = b"pretended-model-bytes" * 1000
    with _MirrorService() as url:
        yield url


def test_catalog_lists_real_presets():
    with TestClient(main.app) as c:
        data = c.get("/api/models/presets").json()
        ids = {p["id"] for p in data["presets"]}
        assert {"yolo11n", "yolov8n", "yolo11s"} <= ids
        for p in data["presets"]:
            assert p["name"] and p["format"] and p["classes"]
            assert p["downloaded"] is False


def test_download_and_register_preset(model_dir, preset_url, monkeypatch):
    # Point the yolo11n preset at the local mirror for a deterministic test.
    monkeypatch.setattr(main, "MODEL_PRESETS", [{**main.MODEL_PRESETS[0], "url": f"{preset_url}/yolo11n.pt"}])
    with TestClient(main.app) as c:
        r = c.post("/api/models/presets/yolo11n/download")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["downloaded"] is True
        assert body["model"] == "yolo11n"
        assert body["requires_validation"] is True
        # file written into MODEL_DIR
        assert (model_dir / "yolo11n.pt").exists()

        # now registered in the registry
        models = c.get("/api/models").json()
        assert any(m["name"] == "yolo11n" and (m["source"] or "").startswith("preset:") for m in models)

        # idempotent: second attempt reports already
        r2 = c.post("/api/models/presets/yolo11n/download")
        assert r2.json()["already"] is True


def test_download_rejects_unknown_preset(model_dir):
    with TestClient(main.app) as c:
        assert c.post("/api/models/presets/nonexistent/download").status_code == 404


def test_delete_model_blocked_when_active(model_dir, preset_url, monkeypatch):
    monkeypatch.setattr(main, "MODEL_PRESETS", [{**main.MODEL_PRESETS[0], "url": f"{preset_url}/yolo11n.pt"}])
    monkeypatch.setattr(main, "MODEL_DIR", model_dir)
    with TestClient(main.app) as c:
        c.post("/api/models/presets/yolo11n/download")
        # Activate is refused without metrics, so set metrics via direct DB to
        # make it activatable, then try to delete the ACTIVE model.
        con = main.db()
        con.execute("UPDATE model_registry SET precision=95, recall=90 WHERE name='yolo11n'")
        con.commit(); con.close()
        assert c.post("/api/models/yolo11n/activate").status_code == 200
        r = c.delete("/api/models/yolo11n")
        assert r.status_code == 409  # active model cannot be deleted
        assert "активн" in r.json()["detail"].lower()


def test_delete_model_removes_registry_and_artifact(model_dir, preset_url, monkeypatch):
    monkeypatch.setattr(main, "MODEL_PRESETS", [{**main.MODEL_PRESETS[0], "url": f"{preset_url}/yolo11n.pt"}])
    monkeypatch.setattr(main, "MODEL_DIR", model_dir)
    with TestClient(main.app) as c:
        c.post("/api/models/presets/yolo11n/download")
        assert (model_dir / "yolo11n.pt").exists()
        r = c.delete("/api/models/yolo11n")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] is True and body["removed_artifact_file"] is True
        # registry entry gone
        assert not any(m["name"] == "yolo11n" for m in c.get("/api/models").json())
        # artifact file removed
        assert not (model_dir / "yolo11n.pt").exists()


def test_delete_model_404_for_unknown(model_dir):
    with TestClient(main.app) as c:
        assert c.delete("/api/models/ghost").status_code == 404


def test_downloaded_preset_cannot_activate_without_metrics(model_dir, preset_url, monkeypatch):
    monkeypatch.setattr(main, "MODEL_PRESETS", [{**main.MODEL_PRESETS[0], "url": f"{preset_url}/yolo11n.pt"}])
    with TestClient(main.app) as c:
        c.post("/api/models/presets/yolo11n/download")
        # activation must refuse because metrics are null (honest guard)
        r = c.post("/api/models/yolo11n/activate")
        assert r.status_code == 409
        assert "метрик" in r.json()["detail"].lower() or "метрики" in r.json()["detail"].lower()
