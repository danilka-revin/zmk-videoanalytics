"""Tests for the in-app updater core (commit channel + legacy release channel).

Uses a local HTTP mirror that mimics the GitHub endpoints used by the updater:

* commit channel (default) — ``/commits/<branch>``, the codeload tarball of a
  commit, the raw ``VERSION`` of a commit and the contents API used to pin the
  downloaded tree to the commit;
* release channel — ``/releases/latest`` + ``SHA256SUMS.txt``.
"""
import hashlib
import io
import json
import sys
import tarfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "updater"))

from core import (
    UPDATE_CHANNEL_ENV,
    UpdateError,
    apply_update,
    current_commit,
    current_version,
    display_version,
    download_and_verify,
    plan_update,
    record_local_build,
    swap_tree,
    tag_commit,
    version_lt,
    write_build_info,
)

TAG = "v9.9.9"
APP_DIR = "zmk-video-analytics-app"
TARBALL = f"zmk-videoanalytics-{TAG}.tar.gz"
HEAD_SHA = "a" * 40
OLD_SHA = "b" * 40


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()


def _archive(project: Path, top: str = APP_DIR) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in project.rglob("*"):
            if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts:
                tar.add(p, arcname=f"{top}/{p.relative_to(project).as_posix()}")
    return buf.getvalue()


class _Handler(SimpleHTTPRequestHandler):
    archive = b""
    # Digest that the checksums file advertises. Kept separate from the
    # served archive so tests can simulate a tampered/attacker-modified file.
    digest = b""
    commit_archive = b""
    head_sha = HEAD_SHA
    head_version = "9.9.9"
    head_message = "Merge pull request #42 from danilka-revin/feature"

    def _send(self, body: bytes, content_type: str = "application/octet-stream") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path.endswith("/releases/latest"):
            self._send(json.dumps({"tag_name": TAG}).encode(), "application/json")
            return
        if path.endswith(f"/{TARBALL}"):
            self._send(self.archive)
            return
        if path.endswith("/SHA256SUMS.txt"):
            self._send(f"{self.digest.decode()}  {TARBALL}\n".encode(), "text/plain")
            return
        if path.endswith(f"/git/ref/tags/{TAG}"):
            body = {"ref": f"refs/tags/{TAG}", "object": {"sha": self.head_sha, "type": "commit"}}
            self._send(json.dumps(body).encode(), "application/json")
            return
        if "/commits/" in path:
            body = {
                "sha": self.head_sha,
                "html_url": f"https://example.invalid/commit/{self.head_sha}",
                "commit": {"message": f"{self.head_message}\n\nbody", "committer": {"date": "2026-01-01T00:00:00Z"}},
            }
            self._send(json.dumps(body).encode(), "application/json")
            return
        if path.startswith("/codeload/"):
            self._send(self.commit_archive)
            return
        if "/raw/" in path and path.endswith("/VERSION"):
            self._send(self.head_version.encode(), "text/plain")
            return
        if "/contents/VERSION" in path and f"ref={self.head_sha}" in query:
            version_file = Path(self.directory) / "project" / "VERSION"
            blob = _blob_sha(version_file.read_bytes())
            self._send(json.dumps({"sha": blob}).encode(), "application/json")
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
    _Handler.commit_archive = payload
    _Handler.head_sha = HEAD_SHA
    _Handler.head_version = "9.9.9"
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(tmp_path)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    t.join(timeout=5)


def _commit_kwargs(mirror: str, **overrides):
    """Updater settings for the local mirror (no git, no real GitHub)."""
    kwargs = {
        "channel": "commit",
        "repo": "danilka-revin/zmk-videoanalytics",
        "branch": "main",
        "commits_api": f"{mirror}/commits/{{branch}}",
        "codeload": f"{mirror}/codeload/{{sha}}.tar.gz",
        "raw_base": f"{mirror}/raw/{{sha}}/{{path}}",
        "use_git": False,
        "timeout": 5.0,
    }
    kwargs.update(overrides)
    return kwargs


def _commit_apply_kwargs(mirror: str, **overrides):
    """Commit-channel settings for apply_update (it also pins the tree)."""
    return _commit_kwargs(mirror, contents_api=f"{mirror}/contents/VERSION?ref={{sha}}", **overrides)


def _release_kwargs(mirror: str, **overrides):
    kwargs = {
        "channel": "release",
        "api_url": f"{mirror}/releases/latest",
        "dl_base": f"{mirror}/releases/download",
        "use_git": False,  # the mirror serves /git/ref/tags/<tag>
    }
    kwargs.update(overrides)
    return kwargs


def _installed_root(tmp_path: Path, version: str, commit: str = "") -> Path:
    root = tmp_path / "root"
    (root / "installers").mkdir(parents=True)
    (root / "VERSION").write_text(version)
    if commit:
        (root / "COMMIT").write_text(commit)
    return root


# --------------------------------------------------------------------------
# Version helpers and the release channel (backwards compatibility)
# --------------------------------------------------------------------------
def test_version_compare():
    assert version_lt("2.2.4", "2.3.0")
    assert not version_lt("2.3.0", "2.3.0")
    assert not version_lt("2.4.0", "2.3.0")
    assert version_lt("1.9.9", "2.0.0")
    assert version_lt("v2.2.4", "2.3.0")
    # Missing patch components are semantically zero, not shorter tuples.
    assert not version_lt("2.3", "2.3.0")
    assert version_lt("", "0.0.1")


def test_display_version_includes_short_commit():
    assert display_version("2.23.0", HEAD_SHA) == "2.23.0+aaaaaaa"
    assert display_version("v2.23.0", "") == "2.23.0"


def test_plan_release_reports_newer(mirror, tmp_path):
    root = _installed_root(tmp_path, "2.2.4")
    plan = plan_update(root, **_release_kwargs(mirror))
    assert plan["channel"] == "release"
    assert plan["update_available"] is True
    assert plan["latest"] == "9.9.9"


def test_plan_release_up_to_date(mirror, tmp_path):
    root = _installed_root(tmp_path, "99.0.0")
    plan = plan_update(root, **_release_kwargs(mirror))
    assert plan["update_available"] is False


def test_full_release_apply_preserves_data(mirror, tmp_path):
    root = _installed_root(tmp_path, "2.2.4")
    (root / ".env").write_text("SECRET=keep\n")
    (root / "data").mkdir()
    (root / "data" / "db.sqlite").write_text("persisted")
    (root / "old.txt").write_text("stale")

    res = apply_update(root, **_release_kwargs(mirror))
    assert res["applied"] is True
    assert res["channel"] == "release"
    assert res["latest"] == "9.9.9"
    assert (root / "VERSION").read_text().strip() == "9.9.9"
    assert (root / "new.txt").read_text() == "fresh"
    assert not (root / "old.txt").exists()
    assert (root / ".env").read_text() == "SECRET=keep\n"
    assert (root / "data" / "db.sqlite").read_text() == "persisted"
    # The applied build is recorded for the panel; the release tag is resolved
    # to its commit, so the install is commit-identified from here on.
    info = (root / "data" / "build-info.json").read_text()
    assert '"channel": "release"' in info
    assert f'"commit": "{HEAD_SHA}"' in info
    assert (root / "COMMIT").read_text().strip() == HEAD_SHA


def test_tag_commit_falls_back_to_git_ls_remote(tmp_path):
    """Without a reachable API the tag is resolved with `git ls-remote`.

    Local bare repository instead of GitHub, so the check stays hermetic.
    """
    import os
    import subprocess

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }

    def git(*args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)

    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "--initial-branch=main", str(remote))
    work = tmp_path / "work"
    git("clone", "-q", str(remote), str(work))
    (work / "VERSION").write_text("2.24.0")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "tagged", cwd=work)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True).stdout.strip()
    git("tag", "-a", "v2.24.0", "-m", "annotated", cwd=work)
    git("tag", "v2.24.0-light", cwd=work)
    git("push", "-q", "origin", "main", "--tags", cwd=work)

    unreachable = "http://127.0.0.1:1/releases/latest"
    assert tag_commit(str(remote), "v2.24.0", api_url=unreachable, timeout=5.0) == sha
    assert tag_commit(str(remote), "v2.24.0-light", api_url=unreachable, timeout=5.0) == sha
    assert tag_commit(str(remote), "v0.0.0", api_url=unreachable, timeout=5.0) == ""


def test_download_and_verify_detects_bad_checksum(mirror, tmp_path):
    # Serve a tampered archive while the checksums file still advertises the
    # original digest, so the SHA256 verification must reject it.
    _Handler.archive = _Handler.archive[:-4] + b"\x00\x00\x00\x00"
    with pytest.raises(UpdateError):
        download_and_verify(f"{mirror}/releases/download", TAG, tmp_path / "dl")


# --------------------------------------------------------------------------
# Commit channel: every commit / merge of the tracked branch is a version
# --------------------------------------------------------------------------
def test_commit_channel_is_the_default(mirror, tmp_path, monkeypatch):
    root = _installed_root(tmp_path, "2.2.4")
    monkeypatch.delenv(UPDATE_CHANNEL_ENV, raising=False)
    plan = plan_update(root, **_commit_kwargs(mirror, channel=None))
    assert plan["channel"] == "commit"


def test_plan_commit_reports_new_commit(mirror, tmp_path):
    root = _installed_root(tmp_path, "9.9.9", OLD_SHA)
    plan = plan_update(root, **_commit_kwargs(mirror))
    assert plan["update_available"] is True
    assert plan["latest_commit"] == HEAD_SHA
    assert plan["latest"] == f"9.9.9+{HEAD_SHA[:7]}"
    assert plan["current"] == f"9.9.9+{OLD_SHA[:7]}"
    assert plan["branch"] == "main"
    assert "feature" in plan["latest_message"]


def test_plan_commit_up_to_date(mirror, tmp_path):
    root = _installed_root(tmp_path, "9.9.9", HEAD_SHA)
    plan = plan_update(root, **_commit_kwargs(mirror))
    assert plan["update_available"] is False
    assert plan["reason"]


def test_same_version_new_commit_still_is_an_update(mirror, tmp_path):
    """The whole point of the commit channel: a merge without a version bump.

    The installed build reports 9.9.9 at an older commit; the branch head also
    says 9.9.9 but is a different commit, so the updater must offer it.
    """
    root = _installed_root(tmp_path, "9.9.9", OLD_SHA)
    _Handler.head_version = "9.9.9"  # the merge did not bump VERSION
    plan = plan_update(root, **_commit_kwargs(mirror))
    assert plan["latest_version"] == "9.9.9"
    assert plan["update_available"] is True
    assert plan["latest_commit"] != plan["current_commit"]


def test_plan_commit_without_commit_marker_installs_branch_head(mirror, tmp_path):
    # A legacy release install has no COMMIT file (and no .git): it must still
    # pick up the branch head.
    root = _installed_root(tmp_path, "2.2.4")
    plan = plan_update(root, **_commit_kwargs(mirror))
    assert plan["update_available"] is True


def test_plan_commit_never_downgrades_legacy_install(mirror, tmp_path):
    root = _installed_root(tmp_path, "99.0.0")
    plan = plan_update(root, **_commit_kwargs(mirror))
    assert plan["update_available"] is False


def test_apply_commit_update_writes_commit_and_build_info(mirror, tmp_path):
    root = _installed_root(tmp_path, "2.2.4")
    (root / ".env").write_text("SECRET=keep\n")
    (root / "data").mkdir()
    (root / "data" / "db.sqlite").write_text("persisted")
    (root / "old.txt").write_text("stale")
    (root / "COMMIT").write_text(OLD_SHA)

    res = apply_update(root, **_commit_apply_kwargs(mirror))
    assert res["applied"] is True
    assert res["commit"] == HEAD_SHA
    assert res["latest"] == f"9.9.9+{HEAD_SHA[:7]}"
    assert (root / "VERSION").read_text().strip() == "9.9.9"
    assert (root / "COMMIT").read_text().strip() == HEAD_SHA
    assert current_commit(root) == HEAD_SHA
    assert (root / "new.txt").read_text() == "fresh"
    assert not (root / "old.txt").exists()
    assert (root / ".env").read_text() == "SECRET=keep\n"
    assert (root / "data" / "db.sqlite").read_text() == "persisted"
    assert f'"commit": "{HEAD_SHA}"' in (root / "data" / "build-info.json").read_text()

    # A second check must now report "up to date".
    assert plan_update(root, **_commit_kwargs(mirror))["update_available"] is False


def test_apply_commit_update_rejects_archive_that_is_not_the_commit(mirror, tmp_path):
    """The staged VERSION blob must match the commit GitHub reports."""
    root = _installed_root(tmp_path, "2.2.4", OLD_SHA)
    # Serve an archive whose VERSION differs from the real commit tree.
    tampered = tmp_path / "tampered"
    (tampered / "installers").mkdir(parents=True)
    (tampered / "VERSION").write_text("6.6.6")
    _Handler.commit_archive = _archive(tampered)
    with pytest.raises(UpdateError, match="does not match commit"):
        apply_update(root, **_commit_apply_kwargs(mirror))


def test_apply_commit_update_requires_a_reachable_branch(mirror, tmp_path):
    root = _installed_root(tmp_path, "2.2.4", OLD_SHA)
    with pytest.raises(UpdateError):
        apply_update(root, **_commit_kwargs(mirror, commits_api=f"{mirror}/missing/{{branch}}"))


def test_write_build_info_keeps_commit_on_swap(tmp_path):
    root = tmp_path / "root"
    staged = tmp_path / "zmk-videoanalytics"
    (root / "data").mkdir(parents=True)
    (staged / "installers").mkdir(parents=True)
    (root / "VERSION").write_text("1.0.0")
    (root / "COMMIT").write_text(OLD_SHA)
    (staged / "VERSION").write_text("2.0.0")
    (staged / "new.txt").write_text("fresh")

    swap_tree(staged, root)
    # A source archive does not ship COMMIT, but the updater-owned file must
    # survive the swap and be rewritten with the new build.
    assert (root / "COMMIT").read_text().strip() == OLD_SHA
    write_build_info(root, version="2.0.0", commit=HEAD_SHA, branch="main")
    assert (root / "COMMIT").read_text().strip() == HEAD_SHA
    assert (root / "VERSION").read_text().strip() == "2.0.0"


def test_current_version_reads_file(tmp_path):
    root = tmp_path / "root"
    (root / "installers").mkdir(parents=True)
    (root / "VERSION").write_text("3.1.4")
    assert current_version(root) == "3.1.4"
    (root / "VERSION").write_text("  \n")
    assert current_version(root) == "0.0.0"


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


def test_app_endpoints_render(mirror, tmp_path, monkeypatch):
    """Sanity: the FastAPI app imports and the endpoints respond (no network)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "updater"))
    root = _installed_root(tmp_path, "9.9.9", OLD_SHA)
    os_env = {
        "UPDATE_ROOT": str(root),
        "ZMK_UPDATE_TOKEN": "secret",
        "ZMK_UPDATE_CHANNEL": "commit",
        "ZMK_UPDATE_BRANCH": "main",
        "ZMK_UPDATE_NO_GIT": "1",
        "ZMK_UPDATE_COMMITS_API": f"{mirror}/commits/{{branch}}",
        "ZMK_UPDATE_CODELOAD": f"{mirror}/codeload/{{sha}}.tar.gz",
        "ZMK_UPDATE_RAW_BASE": f"{mirror}/raw/{{sha}}/{{path}}",
        "ZMK_UPDATE_CONTENTS_API": f"{mirror}/contents/VERSION?ref={{sha}}",
    }
    for key, value in os_env.items():
        monkeypatch.setenv(key, value)
    import app as updater_app
    from fastapi.testclient import TestClient

    client = TestClient(updater_app.app)
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["current"] == f"9.9.9+{OLD_SHA[:7]}"
    s = client.get("/status", headers={"X-Update-Token": "secret"})
    assert s.status_code == 200
    assert s.json()["latest_commit"] == HEAD_SHA
    bad = client.get("/status", headers={"X-Update-Token": "wrong"})
    assert bad.status_code == 403


def test_head_commit_prefers_git_ls_remote(tmp_path):
    """`git ls-remote` is tried first: it does not consume the GitHub quota.

    A local bare repository stands in for the remote, so the check stays
    hermetic while exercising the code path the updater container uses.
    """
    import os
    import subprocess

    from core import head_commit, ls_remote_head

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }

    def git(*args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)

    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "--initial-branch=main", str(remote))
    work = tmp_path / "work"
    git("clone", "-q", str(remote), str(work))
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)
    (work / "VERSION").write_text("9.9.9")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "first", cwd=work)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True).stdout.strip()
    git("push", "-q", "origin", "main", cwd=work)

    assert ls_remote_head(str(remote), "main") == sha
    head = head_commit(
        repo=str(remote),
        branch="main",
        # Unreachable API: metadata is a nicety, the commit must still resolve.
        commits_api="http://127.0.0.1:1/commits/{branch}",
        use_git=True,
        timeout=5.0,
    )
    assert head is not None and head["sha"] == sha
    # A branch that does not exist anywhere resolves to nothing.
    assert head_commit(repo=str(remote), branch="missing", commits_api="http://127.0.0.1:1/commits/{branch}", timeout=5.0) is None


def test_record_local_build_skips_when_nothing_changed(tmp_path):
    """The updater refreshes build-info.json on start, but keeps installed_at.

    This covers a plain `git pull && docker compose up -d --build`: the tree
    changed, so the panel must report the new commit without waiting for the
    next applied update.
    """
    root = _installed_root(tmp_path, "2.24.0", HEAD_SHA)
    first = record_local_build(root, branch="main")
    assert first is not None
    assert first["commit"] == HEAD_SHA
    assert first["display"] == f"2.24.0+{HEAD_SHA[:7]}"
    assert record_local_build(root, branch="main") is None  # nothing changed

    (root / "COMMIT").write_text(OLD_SHA)
    second = record_local_build(root, branch="main")
    assert second is not None and second["commit"] == OLD_SHA
    assert second["installed_at"] >= first["installed_at"]


def test_record_local_build_without_a_commit_is_a_no_op(tmp_path):
    root = _installed_root(tmp_path, "2.24.0")
    assert record_local_build(root) is None
    assert not (root / "COMMIT").exists()
