"""Core of the ZMK Vision self-update logic (pure, testable).

This module contains no network wiring: it is driven by the FastAPI app
in app.py and can be unit-tested directly with a local mirror. It mirrors
the behaviour of the host-level auto-update scripts (auto-update.sh / .ps1)
but also allows triggering an update from the Web panel.

The update flow:
  1. read the current version from <root>/VERSION
  2. fetch latest release tag + checksums from a GitHub-like endpoint
  3. if newer: download the .tar.gz archive to a temporary dir
  4. verify the SHA256 checksum against the release checksums file
  5. extract it and swap the files into <root>, preserving runtime data
     (.env, ./data, databases, .zmk-profiles) and ignoring build artifacts
     (node_modules, dist, .git)
"""
from __future__ import annotations

import hashlib
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any

import httpx

# Paths that must never be overwritten during an update.
PROTECTED_NAMES = {".env", ".zmk-profiles"}
PROTECTED_DIRS = {"data"}
PROTECTED_SUFFIXES = {".db"}
SKIP_PARTS = {".git", "node_modules", "dist", "__pycache__", ".pytest_cache"}

DEFAULT_REPO = "danilka-revin/zmk-videoanalytics"
DEFAULT_API = "https://api.github.com/repos/{repo}/releases/latest"
DEFAULT_DL = "https://github.com/{repo}/releases/download"


def current_version(root: Path) -> str:
    f = root / "VERSION"
    if f.is_file():
        v = f.read_text().strip()
        if v:
            return v
    return "0.0.0"


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


def latest_version(api_url: str, timeout: float = 20.0) -> str | None:
    try:
        resp = httpx.get(api_url, headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
        resp.raise_for_status()
        tag = resp.json().get("tag_name")
    except (httpx.HTTPError, ValueError):
        return None
    return tag.strip() if isinstance(tag, str) and tag.strip() else None


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

    with httpx.stream("GET", f"{dl_base}/{tag}/{base}.tar.gz", timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        with archive.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)

    sums_path = dest_dir / "SHA256SUMS.txt"
    with httpx.stream("GET", f"{dl_base}/{tag}/SHA256SUMS.txt", timeout=60, follow_redirects=True) as resp:
        resp.raise_for_status()
        sums_path.write_bytes(resp.read())

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
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        if rel.name in PROTECTED_NAMES:
            continue
        if any(part in PROTECTED_DIRS for part in rel.parts):
            continue
        if any(rel.name.endswith(s) for s in PROTECTED_SUFFIXES):
            continue
        if rel not in staged_rel:
            p.unlink()

    # Refresh the launchers/updater files themselves.
    for name in ("start.sh", "start.ps1", "installers"):
        pass  # handled by the generic copy above
    return written


class UpdateError(RuntimeError):
    pass


def plan_update(
    root: Path,
    *,
    api_url: str | None = None,
    dl_base: str | None = None,
    repo: str = DEFAULT_REPO,
) -> dict[str, Any]:
    """Return the status of a possible update (no filesystem mutation)."""
    api_url = api_url or DEFAULT_API.format(repo=repo)
    cur = current_version(root)
    latest = latest_version(api_url)
    latest_plain = (latest or "").lstrip("v")
    available = bool(latest_plain) and version_lt(cur, latest_plain)
    return {
        "current": cur,
        "latest": latest_plain or "",
        "latest_tag": latest or "",
        "update_available": available,
        "release_url": f"https://github.com/{repo}/releases/tag/{latest}" if latest else "",
    }


def apply_update(
    root: Path,
    *,
    repo: str = DEFAULT_REPO,
    api_url: str | None = None,
    dl_base: str | None = None,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """Perform the full update and swap, returning a result summary."""
    api_url = api_url or DEFAULT_API.format(repo=repo)
    dl_base = dl_base or DEFAULT_DL.format(repo=repo)
    cur = current_version(root)
    latest = latest_version(api_url)
    if not latest:
        raise UpdateError("could not reach the release feed (offline or rate-limited)")
    latest_plain = latest.lstrip("v")
    if not version_lt(cur, latest_plain):
        return {"applied": False, "current": cur, "latest": latest_plain, "reason": "up_to_date"}

    import tempfile

    temp_options: dict[str, str] = {"prefix": "zmk-upd-"}
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        temp_options["dir"] = str(work_dir)
    with tempfile.TemporaryDirectory(**temp_options) as td:
        work = Path(td)
        archive = download_and_verify(dl_base, latest, work / "dl")
        staged = extract_to_staging(archive, work / "ex")
        written = swap_tree(staged, root)

    return {"applied": True, "current": cur, "latest": latest_plain, "written": written}
