"""Tests for the in-app updater core (download -> SHA256 -> swap).

Uses a local HTTP mirror that mimics the GitHub Releases API + download
URLs, so the real (non-fake) update path is exercised without network.
"""
import hashlib
import io
import sys
import tarfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "updater"))

from core import (
    UpdateError,
    apply_update,
    current_version,
    download_and_verify,
    plan_update,
    swap_tree,
    version_lt,
)

TAG = "v9.9.9"
APP_DIR = "zmk-videoanalytics"
TARBALL = f"zmk-videoanalytics-{TAG}.tar.gz"


def _archive(project: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in project.rglob("*"):
            if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts:
                tar.add(p, arcname=f"{APP_DIR}/{p.relative_to(project).as_posix()}")
    return buf.getvalue()


class _Handler(SimpleHTTPRequestHandler):
    archive = b""
    # Digest that the checksums file advertises. Kept separate from the
    # served archive so tests can simulate a tampered/attacker-modified file.
    digest = b""

    def do_GET(self):
        if "/releases/latest" in self.path:
            body = b'{"tag_name": "%s"}' % TAG.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if f"/{TARBALL}" in self.path:
            body = self.archive
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "SHA256SUMS.txt" in self.path:
            body = f"{self.digest.decode()}  {TARBALL}\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def mirror(tmp_path):
    project = tmp_path / "project"
    (project / "installers").mkdir(parents=True)
    (project / "VERSION").write_text("9.9.9")
    (project / "new.txt").write_text("fresh")
    (project / "data").mkdir()
    (project / "data" / "db.sqlite").write_text("newer-than-root")
    payload = _archive(project)
    _Handler.archive = payload
    _Handler.digest = hashlib.sha256(payload).hexdigest().encode()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(tmp_path)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    t.join(timeout=5)


def test_version_compare():
    assert version_lt("2.2.4", "2.3.0")
    assert not version_lt("2.3.0", "2.3.0")
    assert not version_lt("2.4.0", "2.3.0")
    assert version_lt("1.9.9", "2.0.0")
    assert version_lt("v2.2.4", "2.3.0")
    # Missing patch components are semantically zero, not shorter tuples.
    assert not version_lt("2.3", "2.3.0")
    assert version_lt("", "0.0.1")


def test_plan_update_reports_newer(mirror, tmp_path):
    root = tmp_path / "root"
    (root / "installers").mkdir(parents=True)
    (root / "VERSION").write_text("2.2.4")
    plan = plan_update(root, api_url=f"{mirror}/releases/latest", dl_base=f"{mirror}/releases/download")
    assert plan["update_available"] is True
    assert plan["latest"] == "9.9.9"


def test_plan_update_up_to_date(mirror, tmp_path):
    root = tmp_path / "root"
    (root / "installers").mkdir(parents=True)
    (root / "VERSION").write_text("99.0.0")
    plan = plan_update(root, api_url=f"{mirror}/releases/latest", dl_base=f"{mirror}/releases/download")
    assert plan["update_available"] is False


def test_full_apply_preserves_data(mirror, tmp_path):
    root = tmp_path / "root"
    (root / "installers").mkdir(parents=True)
    (root / "VERSION").write_text("2.2.4")
    (root / ".env").write_text("SECRET=keep\n")
    (root / "data").mkdir()
    (root / "data" / "db.sqlite").write_text("persisted")
    (root / "old.txt").write_text("stale")

    res = apply_update(root, api_url=f"{mirror}/releases/latest", dl_base=f"{mirror}/releases/download")
    assert res["applied"] is True
    assert res["latest"] == "9.9.9"
    assert (root / "VERSION").read_text().strip() == "9.9.9"
    assert (root / "new.txt").read_text() == "fresh"
    assert not (root / "old.txt").exists()
    assert (root / ".env").read_text() == "SECRET=keep\n"
    assert (root / "data" / "db.sqlite").read_text() == "persisted"


def test_download_and_verify_detects_bad_checksum(mirror, tmp_path):
    root = tmp_path / "root"
    (root / "installers").mkdir(parents=True)
    (root / "VERSION").write_text("2.2.4")
    # Serve a tampered archive while the checksums file still advertises the
    # original digest, so the SHA256 verification must reject it.
    _Handler.archive = _Handler.archive[:-4] + b"\x00\x00\x00\x00"
    with pytest.raises(UpdateError):
        download_and_verify(f"{mirror}/releases/download", TAG, tmp_path / "dl")


def test_swap_tree_removes_stale_and_keeps_secrets():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "root"
        staged = base / "zmk-videoanalytics"
        (root / "data").mkdir(parents=True)
        (staged / "data").mkdir(parents=True)
        (root / ".env").write_text("SECRET=1\n")
        (root / "data" / "db.sqlite").write_text("keepme")
        (root / "videoanalytics.db").write_text("keepdb")
        (root / "old.txt").write_text("stale")
        (root / "VERSION").write_text("1.0.0")
        (staged / "new.txt").write_text("fresh")
        (staged / "VERSION").write_text("2.0.0")
        (staged / "data" / "db.sqlite").write_text("should-not-overwrite")

        swap_tree(staged, root)
        assert (root / "new.txt").read_text() == "fresh"
        assert not (root / "old.txt").exists()
        assert (root / "VERSION").read_text().strip() == "2.0.0"
        assert (root / ".env").read_text() == "SECRET=1\n"
        assert (root / "data" / "db.sqlite").read_text() == "keepme"
        assert (root / "videoanalytics.db").read_text() == "keepdb"


def test_current_version_reads_file(tmp_path):
    root = tmp_path / "root"
    (root / "installers").mkdir(parents=True)
    (root / "VERSION").write_text("3.1.4")
    assert current_version(root) == "3.1.4"
    (root / "VERSION").write_text("  \n")
    assert current_version(root) == "0.0.0"


def test_app_endpoints_render(tmp_path):
    """Sanity: the FastAPI app imports and the health endpoint responds."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "updater"))
    import os

    os.environ["UPDATE_ROOT"] = str(tmp_path / "root")
    os.environ["ZMK_UPDATE_TOKEN"] = "secret"
    import app as updater_app
    from fastapi.testclient import TestClient

    client = TestClient(updater_app.app)
    h = client.get("/health")
    assert h.status_code == 200
    s = client.get("/status", headers={"X-Update-Token": "secret"})
    assert s.status_code == 200
    bad = client.get("/status", headers={"X-Update-Token": "wrong"})
    assert bad.status_code == 403
