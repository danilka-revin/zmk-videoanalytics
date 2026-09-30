"""End-to-end test of the self-update pipeline against a local mirror.

The updater is pointed at a lightweight local HTTP server that mimics the
GitHub endpoints, so the full flow is exercised without touching the real
network.

Commit channel (default) — the head commit of the tracked branch is the
version; any commit or merge is picked up:
  installed commit -> head commit -> codeload tarball -> swap -> relaunch.

Release channel (ZMK_UPDATE_CHANNEL=release) — legacy behaviour:
  current version -> latest release -> tar.gz -> verify SHA256 -> swap.
"""
import hashlib
import io
import json
import os
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
NEW_SHA = "c" * 40
OLD_SHA = "d" * 40


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
    commit_archive = b""
    head_sha = NEW_SHA
    head_version = "9.9.9"

    def _send(self, body: bytes, content_type: str = "application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, _query = self.path.partition("?")
        if "/releases/latest" in path:
            self._send(b'{"tag_name": "%s"}' % TAG.encode(), "application/json")
            return
        if f"/{TARBALL}" in path:
            self._send(self.archive)
            return
        if "SHA256SUMS.txt" in path:
            self._send(f"{hashlib.sha256(self.archive).hexdigest()}  {TARBALL}\n".encode(), "text/plain")
            return
        if "/commits/" in path:
            body = json.dumps({"sha": self.head_sha, "commit": {"message": "merge branch", "committer": {"date": "2026-01-01T00:00:00Z"}}}).encode()
            self._send(body, "application/json")
            return
        if path.startswith("/codeload/"):
            self._send(self.commit_archive)
            return
        if "/raw/" in path and path.endswith("/VERSION"):
            self._send(self.head_version.encode(), "text/plain")
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
    _Handler.commit_archive = _Handler.archive
    _Handler.head_sha = NEW_SHA
    _Handler.head_version = "9.9.9"

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
        "ZMK_UPDATE_CHANNEL": "release",
        "ZMK_API": f"{mirror}/releases/latest",
        "ZMK_DL_BASE": f"{mirror}/releases/download",
        "ZMK_INSTALL_ROOT": str(root),
        "ZMK_MARKER": str(marker),
    }
    # Run the real updater; it should exec the relaunch script (run.sh).
    proc = subprocess.run(
        ["bash", str(UPDATER), "run.sh"],
        cwd=str(tmp_path),
        env={**os.environ, **env},
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
        "ZMK_UPDATE_CHANNEL": "release",
        "ZMK_API": f"{mirror}/releases/latest",
        "ZMK_DL_BASE": f"{mirror}/releases/download",
        "ZMK_INSTALL_ROOT": str(root),
    }
    proc = subprocess.run(
        ["bash", str(UPDATER), "run.sh"],
        cwd=str(tmp_path),
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert "Already up to date" in proc.stdout
    assert not (root / "new-file.txt").exists()


def _commit_env(mirror: str, root: Path, **extra) -> dict:
    """Environment for the commit channel pointed at the local mirror."""
    env = {
        "ZMK_REPO": "danilka-revin/zmk-videoanalytics",
        "ZMK_UPDATE_CHANNEL": "commit",
        "ZMK_UPDATE_BRANCH": "main",
        # The mirror replaces GitHub; do not consult the real remote.
        "ZMK_UPDATE_NO_GIT": "1",
        "ZMK_COMMITS_API": f"{mirror}/commits",
        "ZMK_CODELOAD_BASE": f"{mirror}/codeload",
        "ZMK_RAW_BASE": f"{mirror}/raw",
        "ZMK_INSTALL_ROOT": str(root),
    }
    env.update(extra)
    return env


def _run_updater(env: dict, tmp_path: Path):
    return subprocess.run(
        ["bash", str(UPDATER), "run.sh"],
        cwd=str(tmp_path),
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_commit_update_pipeline(mirror, tmp_path):
    """A new commit on the branch updates the installation end to end."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "VERSION").write_text("9.9.8")
    (root / "COMMIT").write_text(OLD_SHA)
    (root / ".env").write_text("SECRET=keep\n")
    (root / "data").mkdir()
    (root / "data" / "db.sqlite").write_text("persisted-data")
    (root / "stale.txt").write_text("remove-me")
    (root / "run.sh").write_text("OLD\nexit 0\n")

    marker = tmp_path / "marker.txt"
    proc = _run_updater(_commit_env(mirror, root, ZMK_MARKER=str(marker)), tmp_path)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert marker.exists() and marker.read_text().strip() == "UPDATE_APPLIED"
    assert (root / "VERSION").read_text().strip() == "9.9.9"
    assert (root / "COMMIT").read_text().strip() == NEW_SHA
    assert (root / "new-file.txt").read_text() == "fresh"
    assert not (root / "stale.txt").exists()
    assert (root / ".env").read_text() == "SECRET=keep\n"
    assert (root / "data" / "db.sqlite").read_text() == "persisted-data"
    # The applied commit is recorded for the panel/logs.
    info = json.loads((root / "data" / "build-info.json").read_text())
    assert info["commit"] == NEW_SHA
    assert info["branch"] == "main"
    assert info["channel"] == "commit"


def test_commit_update_without_version_bump(mirror, tmp_path):
    """A merge that does not bump VERSION still counts as an update."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "VERSION").write_text("9.9.9")
    (root / "COMMIT").write_text(OLD_SHA)
    (root / "run.sh").write_text("exit 0\n")
    marker = tmp_path / "marker.txt"

    proc = _run_updater(_commit_env(mirror, root, ZMK_MARKER=str(marker)), tmp_path)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert marker.exists()
    assert (root / "COMMIT").read_text().strip() == NEW_SHA


def test_commit_channel_skips_when_commit_matches(mirror, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "VERSION").write_text("9.9.9")
    (root / "COMMIT").write_text(NEW_SHA)
    (root / "run.sh").write_text("exit 0\n")

    proc = _run_updater(_commit_env(mirror, root), tmp_path)

    assert proc.returncode == 0
    assert "Already up to date" in proc.stdout
    assert not (root / "new-file.txt").exists()


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=None if cwd is None else str(cwd), check=True,
                   capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})


def test_git_checkout_follows_its_branch_commit_by_commit(tmp_path):
    """A git installation is moved to the head commit of its own branch.

    No archive download and no GitHub API: a local bare repository stands in
    for the remote, so the check is hermetic (and exercises `git ls-remote`).
    """
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "--initial-branch=main", str(remote))
    work = tmp_path / "work"
    _git("clone", "-q", str(remote), str(work))
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)
    (work / "file.txt").write_text("1")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "first", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)

    install = tmp_path / "install"
    _git("clone", "-q", str(remote), str(install))
    (install / "run.sh").write_text('#!/usr/bin/env bash\necho UPDATED > "$ZMK_MARKER"\n')
    marker = tmp_path / "marker.txt"

    # A new commit (the "merge") reaches the remote.
    (work / "file2.txt").write_text("2")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "second commit", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)

    env = {
        "ZMK_REPO_URL": str(remote),
        "ZMK_UPDATE_BRANCH": "main",
        "ZMK_UPDATE_NO_GIT": "0",
        "ZMK_INSTALL_ROOT": str(install),
        "ZMK_MARKER": str(marker),
    }
    proc = subprocess.run(["bash", str(UPDATER), "run.sh"], cwd=str(install),
                          env={**os.environ, **env}, text=True, capture_output=True,
                          timeout=120, check=False)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert marker.exists() and marker.read_text().strip() == "UPDATED"
    assert (install / "file2.txt").read_text() == "2"
    head = subprocess.run(["git", "-C", str(install), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert (install / "COMMIT").read_text().strip() == head
    info = json.loads((install / "data" / "build-info.json").read_text())
    assert info["commit"] == head and info["branch"] == "main"

    # Nothing new on the branch -> no second relaunch.
    marker.unlink()
    second = subprocess.run(["bash", str(UPDATER), "run.sh"], cwd=str(install),
                            env={**os.environ, **env}, text=True, capture_output=True,
                            timeout=120, check=False)
    assert second.returncode == 0
    assert "Already up to date" in second.stdout
    assert not marker.exists()
