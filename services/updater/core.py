"""Core of the Zovod self-update logic (pure, testable).

This module contains no network wiring: it is driven by the FastAPI app
in app.py and can be unit-tested directly with a local mirror.

Two update channels exist:

``commit`` (default)
    Every commit — including a merge of any branch — counts as a new
    version, exactly like the desktop updater of ``danilka-revin/linux_pcb_app``.
    The installed build is identified by its git commit SHA (``COMMIT`` file
    at the project root); the version shown to operators is
    ``<VERSION>+<short sha>``.

``release`` (legacy, ``ZMK_UPDATE_CHANNEL=release``)
    The previous behaviour: follow GitHub Releases, download the published
    archive and verify its SHA256 against ``SHA256SUMS.txt``.

The update flow (commit channel):
  1. read the current state: <root>/VERSION and <root>/COMMIT (or git HEAD)
  2. ask GitHub for the head commit of the tracked branch (``git ls-remote``
     first — it is not rate-limited — then the REST API)
  3. if the SHAs differ, download the source archive of that commit
     (codeload tarball) and extract it into a staging directory
  4. swap the files into <root>, preserving runtime data (.env, ./data,
     databases, .zmk-profiles) and ignoring build artifacts
  5. record the new commit in <root>/COMMIT and <root>/data/build-info.json
     so the panel can show which commit is running
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv lists, never a shell
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# Paths that must never be overwritten during an update.
PROTECTED_NAMES = {".env", ".zmk-profiles"}
PROTECTED_DIRS = {"data"}
PROTECTED_SUFFIXES = {".db"}
SKIP_PARTS = {".git", "node_modules", "dist", "__pycache__", ".pytest_cache"}
# Updater-owned files: they are written by the updater itself, so a source
# archive that does not contain them must not delete them on swap.
UPDATER_OWNED_NAMES = {"COMMIT"}

DEFAULT_REPO = "danilka-revin/zmk-videoanalytics"
DEFAULT_BRANCH = "main"
DEFAULT_CHANNEL = "commit"
DEFAULT_API = "https://api.github.com/repos/{repo}/releases/latest"
DEFAULT_DL = "https://github.com/{repo}/releases/download"
DEFAULT_COMMITS_API = "https://api.github.com/repos/{repo}/commits/{branch}"
DEFAULT_COMPARE_API = "https://api.github.com/repos/{repo}/compare/{base}...{head}"
DEFAULT_CODELOAD = "https://codeload.github.com/{repo}/tar.gz/{sha}"
DEFAULT_RAW = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"
DEFAULT_REPO_URL = "https://github.com/{repo}.git"

UPDATE_CHANNEL_ENV = "ZMK_UPDATE_CHANNEL"
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)*$")
BUILD_INFO_FILE = Path("data") / "build-info.json"
RELEASE_CHANNELS = {"release", "releases", "tag", "tags"}


class UpdateError(RuntimeError):
    """Raised when an update cannot be planned, downloaded or applied."""


# --------------------------------------------------------------------------
# Local build state (VERSION / COMMIT / data/build-info.json)
# --------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def current_version(root: Path) -> str:
    """Semantic version of the installed tree (``VERSION`` file)."""
    value = _read_text(root / "VERSION")
    return value or "0.0.0"


def current_commit(root: Path) -> str:
    """Commit SHA of the installed tree: ``COMMIT`` file, then git HEAD."""
    value = _read_text(root / "COMMIT").lower()
    if SHA_RE.match(value):
        return value
    return git_head(root)


def git_head(root: Path, timeout: float = 10.0) -> str:
    """``git rev-parse HEAD`` for a git checkout (empty when unavailable)."""
    if not (root / ".git").exists() or shutil.which("git") is None:
        return ""
    try:
        out = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return ""
    return out if SHA_RE.match(out) else ""


def short_commit(value: str) -> str:
    value = (value or "").strip().lower()
    return value[:7] if SHA_RE.match(value) else ""


def display_version(version: str, commit: str) -> str:
    """Operator-facing version: ``2.23.0+1a2b3c4`` (semver build metadata)."""
    version = (version or "").strip().lstrip("v") or "0.0.0"
    short = short_commit(commit)
    return f"{version}+{short}" if short else version


def local_state(root: Path) -> dict[str, str]:
    """Version + commit of the installed tree, ready for the panel.

    ``VERSION`` on disk always wins for the human-readable part. The commit is
    taken from ``COMMIT``/git; only an install that has neither (a plain
    release archive installed before commit tracking existed) falls back to
    the commit recorded in ``data/build-info.json``.
    """
    version = current_version(root)
    commit = current_commit(root) or str(read_build_info(root).get("commit") or "")
    return {
        "version": version,
        "commit": commit,
        "short": short_commit(commit),
        "display": display_version(version, commit),
    }


def read_build_info(root: Path) -> dict[str, Any]:
    """Last recorded build info (``data/build-info.json``), ``{}`` if absent."""
    try:
        data = json.loads((root / BUILD_INFO_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_build_info(
    root: Path,
    *,
    version: str,
    commit: str,
    branch: str = "",
    channel: str = DEFAULT_CHANNEL,
    message: str = "",
) -> dict[str, Any]:
    """Record the installed commit in ``COMMIT`` and ``data/build-info.json``."""
    commit = (commit or "").strip().lower()
    version = (version or "").strip() or current_version(root)
    payload: dict[str, Any] = {
        "version": version,
        "commit": commit if SHA_RE.match(commit) else "",
        "short": short_commit(commit),
        "display": display_version(version, commit),
        "branch": branch,
        "channel": channel,
        "message": (message or "").splitlines()[0][:200] if message else "",
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if payload["commit"]:
        (root / "COMMIT").write_text(payload["commit"] + "\n", encoding="utf-8")
    path = root / BUILD_INFO_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return payload


def record_local_build(
    root: Path,
    *,
    branch: str = "",
    channel: str = DEFAULT_CHANNEL,
) -> dict[str, Any] | None:
    """Refresh ``COMMIT``/``build-info.json`` for the mounted tree.

    Called when the updater container starts so that a plain
    ``git pull && docker compose up -d --build`` (or a re-tagged checkout) is
    reported by the panel with its real commit. Returns the new info, or
    ``None`` when nothing changed (or the tree has no commit to record).
    """
    version = current_version(root)
    commit = current_commit(root)
    if not commit:
        return None
    info = read_build_info(root)
    if info.get("commit") == commit and str(info.get("version") or "") == version and info.get("installed_at"):
        return None
    return write_build_info(
        root,
        version=version,
        commit=commit,
        branch=branch or str(info.get("branch") or ""),
        channel=channel or str(info.get("channel") or DEFAULT_CHANNEL),
        message=str(info.get("message") or ""),
    )


# --------------------------------------------------------------------------
# Version helpers (kept for the release channel and the tests)
# --------------------------------------------------------------------------
def _version_parts(value: str) -> tuple[int, int, int]:
    """Return a tolerant three-part numeric version for release comparison."""
    cleaned = re.sub(r"[^0-9.]", "", (value or ""))
    pieces: list[int] = []
    for part in cleaned.split("."):
        if not part:
            continue
        pieces.append(int(part))
        if len(pieces) == 3:
            break
    normalized = (pieces + [0, 0, 0])[:3]
    return normalized[0], normalized[1], normalized[2]


def version_lt(a: str, b: str) -> bool:
    """Numeric semver comparison (a < b), including short/malformed values."""
    return _version_parts(a) < _version_parts(b)


# --------------------------------------------------------------------------
# Talking to GitHub
# --------------------------------------------------------------------------
def _headers(token: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _repo_url(repo: str) -> str:
    """Git remote URL: an explicit URL/path wins, otherwise ``owner/name``.

    ``ZMK_UPDATE_REPO`` normally holds ``danilka-revin/zmk-videoanalytics``; a
    full URL, an ssh remote or a local path (mirrors, tests) is passed through
    untouched so both ``git ls-remote`` and the REST API can use it.
    """
    if "://" in repo or repo.startswith(("git@", "/", "./", "../")) or re.match(r"^[A-Za-z]:[\\/]", repo):
        return repo
    return DEFAULT_REPO_URL.format(repo=repo)


def _slug(repo: str) -> str:
    """``danilka-revin/zmk-videoanalytics`` out of a repository URL."""
    cleaned = re.sub(r"^https?://", "", repo).removesuffix(".git").strip("/")
    return cleaned.removeprefix("github.com/")


def ls_remote_head(repo: str, branch: str = DEFAULT_BRANCH, timeout: float = 20.0) -> str:
    """Head commit of a branch via ``git ls-remote`` (no GitHub API quota)."""
    if not branch or shutil.which("git") is None:
        return ""
    try:
        out = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "ls-remote", "--heads", _repo_url(repo), f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"^([0-9a-f]{40})\s+refs/heads/", out, re.MULTILINE)
    return match.group(1).lower() if match else ""


def ls_remote_tag(repo: str, tag: str, timeout: float = 20.0) -> str:
    """Commit a tag points at via ``git ls-remote`` (annotated tags resolved)."""
    if not tag or shutil.which("git") is None:
        return ""
    refs = [f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"]
    try:
        out = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "ls-remote", _repo_url(repo), *refs],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    found = {ref: sha.lower() for sha, ref in re.findall(r"^([0-9a-f]{40})\s+(\S+)$", out, re.MULTILINE)}
    return found.get(f"refs/tags/{tag}^{{}}", "") or found.get(f"refs/tags/{tag}", "")


def tag_commit(
    repo: str,
    tag: str,
    *,
    api_url: str | None = None,
    token: str | None = None,
    timeout: float = 20.0,
    use_git: bool = True,
) -> str:
    """Commit a release tag points at (annotated tags are dereferenced).

    The legacy release channel records this after applying an update, so an
    install made from a release archive still knows its commit and can be
    compared by commit from then on.
    """
    if not tag:
        return ""
    api = (api_url or DEFAULT_API.format(repo=repo)).split("/releases/", 1)[0].rstrip("/")
    try:
        resp = httpx.get(f"{api}/git/ref/tags/{tag}", headers=_headers(token), timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        obj = data.get("object") if isinstance(data, dict) else None
        if isinstance(obj, dict) and obj.get("type") == "tag" and obj.get("url"):
            resp = httpx.get(str(obj["url"]), headers=_headers(token), timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
            obj = data.get("object") if isinstance(data, dict) else None
        sha = str((obj or {}).get("sha") or "").lower()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except (httpx.HTTPError, ValueError):
        pass
    return ls_remote_tag(repo, tag, timeout=timeout) if use_git else ""


def latest_version(api_url: str, timeout: float = 20.0, token: str | None = None) -> str | None:
    """Latest release tag (legacy release channel)."""
    try:
        resp = httpx.get(api_url, headers=_headers(token), timeout=timeout)
        resp.raise_for_status()
        tag = resp.json().get("tag_name")
    except (httpx.HTTPError, ValueError):
        return None
    return tag.strip() if isinstance(tag, str) and tag.strip() else None


def _api_commit(commits_api: str, repo: str, branch: str, token: str | None, timeout: float) -> dict[str, Any] | None:
    """Commit object of the branch head from the GitHub REST API."""
    url = commits_api.format(repo=_slug(repo), branch=branch)
    try:
        resp = httpx.get(url, headers=_headers(token), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    sha = str(data.get("sha") or "").lower()
    if not SHA_RE.match(sha):
        return None
    commit = data.get("commit") or {}
    return {
        "sha": sha,
        "message": str((commit.get("message") or "").splitlines()[0])[:200] if commit.get("message") else "",
        "date": str((commit.get("committer") or {}).get("date") or ""),
        "url": str(data.get("html_url") or ""),
    }


def head_commit(
    *,
    repo: str = DEFAULT_REPO,
    branch: str = DEFAULT_BRANCH,
    commits_api: str | None = None,
    token: str | None = None,
    timeout: float = 20.0,
    use_git: bool = True,
) -> dict[str, Any] | None:
    """Head commit of the tracked branch.

    ``git ls-remote`` is tried first because it does not consume the GitHub
    API quota (60 requests/hour for anonymous callers); the REST API is the
    fallback and also supplies the commit message/date for the panel.
    """
    commits_api = commits_api or DEFAULT_COMMITS_API
    sha = ls_remote_head(repo, branch, timeout) if use_git else ""
    meta: dict[str, Any] | None = None
    if not sha:
        meta = _api_commit(commits_api, repo, branch, token, timeout)
        if meta is None:
            return None
        sha = meta["sha"]
    if meta is None:
        # Metadata is a nicety: an exhausted API quota must not break updates.
        meta = _api_commit(commits_api, repo, branch, token, timeout) or {"sha": sha, "message": "", "date": "", "url": ""}
        if meta["sha"] != sha:
            meta = {"sha": sha, "message": "", "date": "", "url": ""}
    return meta


def version_at(
    *,
    repo: str = DEFAULT_REPO,
    sha: str,
    raw_base: str | None = None,
    timeout: float = 20.0,
) -> str:
    """``VERSION`` file of a commit (``""`` when unknown)."""
    if not SHA_RE.match((sha or "").strip()):
        return ""
    url = (raw_base or DEFAULT_RAW).format(repo=_slug(repo), sha=sha, ref=sha, path="VERSION")
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        value = resp.text.strip()
    except (httpx.HTTPError, ValueError):
        return ""
    return value if VERSION_RE.match(value) else ""


def commits_behind(
    *,
    repo: str = DEFAULT_REPO,
    older: str,
    newer: str,
    compare_api: str | None = None,
    token: str | None = None,
    timeout: float = 15.0,
) -> int | None:
    """How many commits the installed build is behind the branch head."""
    if not (SHA_RE.match(older or "") and SHA_RE.match(newer or "")) or older == newer:
        return None
    url = (compare_api or DEFAULT_COMPARE_API).format(repo=_slug(repo), base=older, head=newer)
    try:
        resp = httpx.get(url, headers=_headers(token), timeout=timeout)
        resp.raise_for_status()
        value = resp.json().get("ahead_by")
    except (httpx.HTTPError, ValueError):
        return None
    return int(value) if isinstance(value, int) else None


def _blob_sha(data: bytes) -> str:
    """Git blob hash of an in-memory file (used to pin the downloaded tree)."""
    header = f"blob {len(data)}\0".encode()
    # Git object ids are SHA1 by definition; this is content addressing, not a
    # security primitive of its own (bandit: usedforsecurity=False).
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def remote_blob_sha(
    *,
    repo: str = DEFAULT_REPO,
    sha: str,
    path: str = "VERSION",
    contents_api: str | None = None,
    token: str | None = None,
    timeout: float = 20.0,
) -> str:
    """Git blob hash of a file inside a commit, as reported by GitHub."""
    if not SHA_RE.match((sha or "").strip()):
        return ""
    template = contents_api or "https://api.github.com/repos/{repo}/contents/{path}?ref={sha}"
    url = template.format(repo=_slug(repo), sha=sha, path=path)
    try:
        resp = httpx.get(url, headers=_headers(token), timeout=timeout)
        resp.raise_for_status()
        value = str(resp.json().get("sha") or "").lower()
    except (httpx.HTTPError, ValueError):
        return ""
    return value if SHA_RE.match(value) else ""


# --------------------------------------------------------------------------
# Downloading / verifying / staging
# --------------------------------------------------------------------------
def _download(url: str, dest: Path, timeout: float = 900.0) -> Path:
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    if dest.stat().st_size == 0:
        raise UpdateError(f"downloaded file is empty: {url}")
    return dest


def download_commit_archive(
    dest_dir: Path,
    *,
    repo: str = DEFAULT_REPO,
    sha: str,
    codeload: str | None = None,
    timeout: float = 900.0,
) -> Path:
    """Download the source archive (tar.gz) of one commit from GitHub."""
    sha = (sha or "").strip().lower()
    if not SHA_RE.match(sha):
        raise UpdateError(f"invalid commit sha: {sha!r}")
    url = (codeload or DEFAULT_CODELOAD).format(repo=_slug(repo), sha=sha, ref=sha)
    dest_dir.mkdir(parents=True, exist_ok=True)
    return _download(url, dest_dir / f"zmk-videoanalytics-{sha}.tar.gz", timeout)


def _strip_sums(path: Path) -> dict[str, str]:
    """Parse a SHA256SUMS.txt style file into {filename: sha256}."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[1]] = parts[0].lower()
    return out


def download_and_verify(
    dl_base: str,
    tag: str,
    dest_dir: Path,
    timeout: float = 900.0,
) -> Path:
    """Download <tag>.tar.gz + checksums, verify SHA256, return the archive path."""
    base = f"zmk-videoanalytics-{tag}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / f"{base}.tar.gz"

    _download(f"{dl_base}/{tag}/{base}.tar.gz", archive, timeout)
    sums_path = dest_dir / "SHA256SUMS.txt"
    _download(f"{dl_base}/{tag}/SHA256SUMS.txt", sums_path, 60)

    expected = _strip_sums(sums_path).get(f"{base}.tar.gz")
    if not expected:
        raise UpdateError(f"no checksum for {base}.tar.gz in SHA256SUMS.txt")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise UpdateError(f"SHA256 mismatch: expected {expected}, got {actual}")
    return archive


def _safe_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """Validate an archive member before extraction (path-traversal guard)."""
    parts = Path(member.name).parts
    if member.name.startswith("/") or ".." in parts:
        raise UpdateError(f"unsafe archive member path: {member.name}")
    if member.isdev() or member.issym() or member.islnk():
        raise UpdateError(f"unsafe archive member (link/device): {member.name}")
    return member


def extract_to_staging(archive: Path, work_dir: Path) -> Path:
    """Extract archive into work_dir, return the nested project directory.

    Members are validated for absolute/../ paths and links before being
    written, and each file is extracted individually (no extractall), which
    avoids the unsafe tar-extraction pattern. We validate members ourselves
    instead of using TarFile's ``filter=`` argument so this works on Python
    3.11 as well as the project's CI/runtime Python 3.12+.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            _safe_member(member)
        for member in members:
            tar.extract(member, path=work_dir)
    candidates = [p for p in work_dir.iterdir() if p.is_dir() and (p / "VERSION").is_file()]
    if not candidates:
        raise UpdateError("archive has no project directory with VERSION")
    # Prefer a directory named zmk-videoanalytics if present.
    for c in candidates:
        if c.name == "zmk-videoanalytics":
            return c
    return candidates[0]


def _should_copy(src: Path, rel: Path) -> bool:
    """Decide whether a staged file should overwrite the installed tree."""
    if any(part in SKIP_PARTS for part in rel.parts):
        return False
    if rel.name in PROTECTED_NAMES:
        return False
    if any(part in PROTECTED_DIRS for part in rel.parts):
        return False
    return not any(rel.name.endswith(s) for s in PROTECTED_SUFFIXES)


def _should_keep(rel: Path) -> bool:
    """Files that stay in place even when a source archive does not ship them."""
    if any(part in SKIP_PARTS for part in rel.parts):
        return True
    if rel.name in PROTECTED_NAMES or rel.name in UPDATER_OWNED_NAMES:
        return True
    if any(part in PROTECTED_DIRS for part in rel.parts):
        return True
    return any(rel.name.endswith(s) for s in PROTECTED_SUFFIXES)


def swap_tree(staged: Path, root: Path) -> int:
    """Copy the staged project over the installed root, preserving runtime

    data and secrets, and removing stale files that no longer exist upstream.
    Returns the number of files written.
    """
    staged_files = [p for p in staged.rglob("*") if p.is_file()]
    staged_rel = {p.relative_to(staged): None for p in staged_files}

    written = 0
    for rel in staged_rel:
        src = staged / rel
        if not _should_copy(src, rel):
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        written += 1

    # Remove files that exist in root but no longer exist upstream.
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _should_keep(rel):
            continue
        if rel not in staged_rel:
            p.unlink()

    return written


# --------------------------------------------------------------------------
# Update planning / applying
# --------------------------------------------------------------------------
def normalize_channel(channel: str | None) -> str:
    value = (channel or os.getenv(UPDATE_CHANNEL_ENV) or DEFAULT_CHANNEL).strip().lower()
    return "release" if value in RELEASE_CHANNELS else "commit"


def _plan_release(root: Path, *, repo: str, api_url: str | None, dl_base: str | None, token: str | None) -> dict[str, Any]:
    """Legacy release channel: GitHub Releases + SHA256 of the asset."""
    cur = current_version(root)
    latest = latest_version(api_url or DEFAULT_API.format(repo=repo), token=token)
    latest_plain = (latest or "").lstrip("v")
    available = bool(latest_plain) and version_lt(cur, latest_plain)
    state = local_state(root)
    return {
        "channel": "release",
        "repo": repo,
        "branch": "",
        "current": state["display"],
        "current_version": cur,
        "current_commit": state["commit"],
        "current_short": state["short"],
        "latest": latest_plain,
        "latest_version": latest_plain,
        "latest_commit": "",
        "latest_short": "",
        "latest_message": "",
        "latest_date": "",
        "latest_tag": latest or "",
        "update_available": available,
        "release_url": f"https://github.com/{repo}/releases/tag/{latest}" if latest else "",
        "commits_behind": None,
        "reason": "" if latest_plain else "Служба обновления не смогла получить последний релиз GitHub.",
    }


def _plan_commit(
    root: Path,
    *,
    repo: str,
    branch: str,
    commits_api: str | None,
    compare_api: str | None,
    raw_base: str | None,
    token: str | None,
    timeout: float,
    use_git: bool = True,
) -> dict[str, Any]:
    """Commit channel: every commit/merge of the tracked branch is a version."""
    state = local_state(root)
    head = head_commit(repo=repo, branch=branch, commits_api=commits_api, token=token, timeout=timeout, use_git=use_git)
    base: dict[str, Any] = {
        "channel": "commit",
        "repo": repo,
        "branch": branch,
        "current": state["display"],
        "current_version": state["version"],
        "current_commit": state["commit"],
        "current_short": state["short"],
        "latest": "",
        "latest_version": "",
        "latest_commit": "",
        "latest_short": "",
        "latest_message": "",
        "latest_date": "",
        "latest_tag": "",
        "update_available": False,
        "release_url": "",
        "commits_behind": None,
        "reason": "",
    }
    if head is None:
        base["reason"] = f"Не удалось получить последний коммит ветки {branch} на GitHub (нет сети или лимит запросов)."
        return base

    sha = str(head["sha"])
    head_version = version_at(repo=repo, sha=sha, raw_base=raw_base, timeout=timeout) or state["version"]
    base.update(
        {
            "latest": display_version(head_version, sha),
            "latest_version": head_version,
            "latest_commit": sha,
            "latest_short": short_commit(sha),
            "latest_message": str(head.get("message") or ""),
            "latest_date": str(head.get("date") or ""),
            "latest_tag": short_commit(sha),
            "release_url": str(head.get("url") or f"https://github.com/{repo}/commit/{sha}"),
        }
    )
    if state["commit"]:
        base["update_available"] = state["commit"] != sha
        if not base["update_available"]:
            base["reason"] = "Установлен последний коммит ветки."
    else:
        # A legacy install (release archive without COMMIT) has no commit to
        # compare; fall back to VERSION and never downgrade to an older branch
        # state. Equal versions still count as an update: the point of the
        # commit channel is that the branch head is installed.
        if version_lt(head_version, state["version"]):
            base["reason"] = f"Установленная сборка ({state['version']}) новее ветки {branch} — обновление не требуется."
        else:
            base["update_available"] = True
            base["reason"] = "Локальная сборка не содержит отметки коммита: установится коммит ветки."
    if base["update_available"]:
        base["commits_behind"] = commits_behind(
            repo=repo, older=state["commit"], newer=sha, compare_api=compare_api, token=token, timeout=min(timeout, 15.0)
        )
    return base


def plan_update(
    root: Path,
    *,
    channel: str | None = None,
    repo: str = DEFAULT_REPO,
    branch: str = DEFAULT_BRANCH,
    api_url: str | None = None,
    dl_base: str | None = None,
    commits_api: str | None = None,
    compare_api: str | None = None,
    codeload: str | None = None,
    raw_base: str | None = None,
    token: str | None = None,
    timeout: float = 20.0,
    use_git: bool = True,
) -> dict[str, Any]:
    """Return the status of a possible update (no filesystem mutation)."""
    channel = normalize_channel(channel)
    if channel == "release":
        return _plan_release(root, repo=repo, api_url=api_url, dl_base=dl_base, token=token)
    return _plan_commit(
        root,
        repo=repo,
        branch=branch,
        commits_api=commits_api,
        compare_api=compare_api,
        raw_base=raw_base,
        token=token,
        timeout=timeout,
        use_git=use_git,
    )


def _verify_staged_commit(staged: Path, *, repo: str, sha: str, contents_api: str | None, token: str | None, timeout: float) -> None:
    """Pin the downloaded tree to the requested commit.

    GitHub serves source archives over HTTPS; a cheap extra check compares the
    git blob hash of the staged ``VERSION`` file with the blob hash GitHub
    reports for that commit, so a corrupted or substituted archive is caught
    before it is swapped in. A missing/unreachable API is not fatal: the
    updater must keep working when the anonymous quota is exhausted.
    """
    expected = remote_blob_sha(repo=repo, sha=sha, contents_api=contents_api, token=token, timeout=timeout)
    if not expected:
        return
    version_file = staged / "VERSION"
    if not version_file.is_file():
        raise UpdateError("archive has no VERSION file")
    actual = _blob_sha(version_file.read_bytes())
    if actual != expected:
        raise UpdateError(f"archive does not match commit {sha}: VERSION blob {actual} != {expected}")


def apply_update(
    root: Path,
    *,
    channel: str | None = None,
    repo: str = DEFAULT_REPO,
    branch: str = DEFAULT_BRANCH,
    api_url: str | None = None,
    dl_base: str | None = None,
    commits_api: str | None = None,
    compare_api: str | None = None,
    codeload: str | None = None,
    raw_base: str | None = None,
    contents_api: str | None = None,
    token: str | None = None,
    work_dir: Path | None = None,
    timeout: float = 20.0,
    use_git: bool = True,
) -> dict[str, Any]:
    """Perform the full update and swap, returning a result summary."""
    channel = normalize_channel(channel)

    temp_options: dict[str, str] = {"prefix": "zmk-upd-"}
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        temp_options["dir"] = str(work_dir)

    # --- legacy release channel -------------------------------------------
    if channel == "release":
        api = api_url or DEFAULT_API.format(repo=repo)
        dl = dl_base or DEFAULT_DL.format(repo=repo)
        state = local_state(root)
        cur = current_version(root)
        latest = latest_version(api, token=token)
        if not latest:
            raise UpdateError("could not reach the release feed (offline or rate-limited)")
        latest_plain = latest.lstrip("v")
        if not version_lt(cur, latest_plain):
            return {"applied": False, "channel": "release", "current": state["display"], "latest": latest_plain, "reason": "up_to_date"}
        with tempfile.TemporaryDirectory(**temp_options) as td:
            work = Path(td)
            archive = download_and_verify(dl, latest, work / "dl")
            staged = extract_to_staging(archive, work / "ex")
            written = swap_tree(staged, root)
            # A release archive carries no .git; resolve the tag to its commit
            # so the install is identified by a commit from now on.
            commit = tag_commit(repo, latest, api_url=api_url, token=token, timeout=timeout, use_git=use_git) or current_commit(staged)
            info = write_build_info(root, version=current_version(staged), commit=commit, channel="release")
        return {
            "applied": True,
            "channel": "release",
            "current": state["display"],
            "latest": latest_plain,
            "commit": info["commit"],
            "written": written,
        }

    # --- commit channel ---------------------------------------------------
    plan = _plan_commit(
        root,
        repo=repo,
        branch=branch,
        commits_api=commits_api,
        compare_api=compare_api,
        raw_base=raw_base,
        token=token,
        timeout=timeout,
        use_git=use_git,
    )
    if not plan["latest_commit"]:
        raise UpdateError(plan.get("reason") or "could not reach the branch feed (offline or rate-limited)")
    if not plan["update_available"]:
        return {
            "applied": False,
            "channel": "commit",
            "current": plan["current"],
            "latest": plan["latest"],
            "commit": plan["current_commit"],
            "reason": "up_to_date",
        }

    sha = plan["latest_commit"]
    with tempfile.TemporaryDirectory(**temp_options) as td:
        work = Path(td)
        archive = download_commit_archive(work / "dl", repo=repo, sha=sha, codeload=codeload)
        staged = extract_to_staging(archive, work / "ex")
        _verify_staged_commit(staged, repo=repo, sha=sha, contents_api=contents_api, token=token, timeout=timeout)
        written = swap_tree(staged, root)
        info = write_build_info(
            root,
            version=current_version(staged),
            commit=sha,
            branch=branch,
            channel="commit",
            message=plan["latest_message"],
        )

    return {
        "applied": True,
        "channel": "commit",
        "current": plan["current"],
        "latest": info["display"],
        "commit": sha,
        "branch": branch,
        "written": written,
    }
