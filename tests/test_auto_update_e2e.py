"""End-to-end test of the self-update pipeline against a local mirror.

The updater is pointed at a lightweight local HTTP server that mimics the
GitHub Releases API + download URLs, so the full flow is exercised without
touching the real network:
  current version -> fetch latest -> download zip/tar.gz -> verify SHA256
  -> extract -> swap files in place (preserving data) -> relaunch.
"""
import hashlib
import io
import subprocess
import tarfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "installers" / "auto-update.sh"

TAG = "v9.9.9"
BASE = f"zmk-videoanalytics-{TAG}"
TARBALL = f"{BASE}.tar.gz"
# Real release archives unpack to a top-level directory of this name.
APP_DIR = "zmk-videoanalytics"


def _make_archive_bytes(version_dir: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in version_dir.rglob("*"):
            if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts:
                arc = f"{APP_DIR}/{p.relative_to(version_dir).as_posix()}"
                tar.add(p, arcname=arc)
    return buf.getvalue()


class _Handler(SimpleHTTPRequestHandler):
    archive = b""

    def do_GET(self):
        if self.path.endswith("/releases/latest") or "/releases/latest" in self.path:
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
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "SHA256SUMS.txt" in self.path:
            body = f"{hashlib.sha256(self.archive).hexdigest()}  {TARBALL}\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):  # silence noisy logs
        pass


@pytest.fixture
def mirror(tmp_path):
    # Build the "new release" project tree.
    new = tmp_path / "project"
    new.mkdir()
    (new / "installers").mkdir()
    # Real release archives ship the auto-updater; the apply step relaunches
    # from it, so include the real updater for a faithful end-to-end run.
    (new / "installers" / "auto-update.sh").write_text(UPDATER.read_text())
    (new / "VERSION").write_text("9.9.9")
    (new / "run.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo UPDATE_APPLIED > \"${ZMK_MARKER:-/tmp/zmk-marker}\"\n"
        "exit 0\n"
    )
    (new / "new-file.txt").write_text("fresh")
    (new / "data").mkdir()
    (new / "data" / "db.sqlite").write_text("newer-than-root")

    _Handler.archive = _make_archive_bytes(new)

    handler = partial(_Handler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def test_full_auto_update_pipeline(mirror, tmp_path):
    # The existing installed copy (root) with an older version.
    root = tmp_path / "root"
    root.mkdir()
    (root / "VERSION").write_text("2.2.4")
    (root / ".env").write_text("SECRET=keep\n")
    (root / "data").mkdir()
    (root / "data" / "db.sqlite").write_text("persisted-data")
    (root / "stale.txt").write_text("remove-me")
    (root / "run.sh").write_text("OLD\nexit 0\n")

    marker = tmp_path / "marker.txt"
    env = {
        "ZMK_REPO": "danilka-revin/zmk-videoanalytics",
        "ZMK_API": f"{mirror}/releases/latest",
        "ZMK_DL_BASE": f"{mirror}/releases/download",
        "ZMK_INSTALL_ROOT": str(root),
        "ZMK_MARKER": str(marker),
    }
    # Run the real updater; it should exec the relaunch script (run.sh).
    proc = subprocess.run(
        ["bash", str(UPDATER), "run.sh"],
        cwd=str(tmp_path),
        env={**__import__("os").environ, **env},
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    # relaunch marker written -> the update was applied and program relaunched
    assert marker.exists() and marker.read_text().strip() == "UPDATE_APPLIED"
    # old version swapped for the new one
    assert (root / "VERSION").read_text().strip() == "9.9.9"
    assert (root / "new-file.txt").read_text() == "fresh"
    # stale file removed, secrets + data preserved
    assert not (root / "stale.txt").exists()
    assert (root / ".env").read_text() == "SECRET=keep\n"
    assert (root / "data" / "db.sqlite").read_text() == "persisted-data"


def test_no_update_when_current_is_newest(mirror, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "VERSION").write_text("99.0.0")

    env = {
        "ZMK_REPO": "danilka-revin/zmk-videoanalytics",
        "ZMK_API": f"{mirror}/releases/latest",
        "ZMK_DL_BASE": f"{mirror}/releases/download",
        "ZMK_INSTALL_ROOT": str(root),
    }
    proc = subprocess.run(
        ["bash", str(UPDATER), "run.sh"],
        cwd=str(tmp_path),
        env={**__import__("os").environ, **env},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert "Already up to date" in proc.stdout
    assert not (root / "new-file.txt").exists()
