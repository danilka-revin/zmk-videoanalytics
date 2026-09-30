"""Zovod updater sidecar.

This service runs inside the Docker network with the host project root
bind-mounted at UPDATE_ROOT (default /workspace) and the Docker socket at
/var/run/docker.sock. It performs real, verifiable updates and then redeploys
the application containers so the new code actually runs.

Two channels are supported (``ZMK_UPDATE_CHANNEL``):

``commit`` (default)
    Every commit — including a merge of any branch — is a new version,
    exactly like the desktop updater of ``danilka-revin/linux_pcb_app``: the
    head commit of ``ZMK_UPDATE_BRANCH`` (default ``main``) is compared with
    the installed commit (``COMMIT`` file / git HEAD) and its source archive
    is swapped in. The download is pinned to the commit that was checked.

``release`` (legacy)
    Follow GitHub Releases, verify the published SHA256 checksum of the
    archive and swap it in.

Both channels preserve .env, ./data, databases and the saved Compose
profiles. The Web panel reaches this service through the backend, which
proxies GET /api/update/status and POST /api/update/apply here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess  # nosec B404  (controlled Docker CLI, invoked without shell)
import threading
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from core import UpdateError, apply_update, local_state, plan_update, record_local_build
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

ROOT = Path(os.getenv("UPDATE_ROOT", "/workspace"))
REPO = os.getenv("ZMK_UPDATE_REPO", "danilka-revin/zmk-videoanalytics")
TOKEN = os.getenv("ZMK_UPDATE_TOKEN", "").strip()
API_URL = os.getenv("ZMK_UPDATE_API", "") or None
DL_BASE = os.getenv("ZMK_UPDATE_DL_BASE", "") or None

# --- Commit channel (default) ------------------------------------------------
# Any commit or merge pushed to BRANCH counts as a new version of the program.
BRANCH = os.getenv("ZMK_UPDATE_BRANCH", "main").strip() or "main"
CHANNEL = os.getenv("ZMK_UPDATE_CHANNEL", "commit").strip() or "commit"
# Optional endpoints: mirrors and tests point these at a local server, and a
# GitHub token raises the anonymous API quota (60 requests/hour) if needed.
COMMITS_API = os.getenv("ZMK_UPDATE_COMMITS_API", "") or None
COMPARE_API = os.getenv("ZMK_UPDATE_COMPARE_API", "") or None
CODELOAD = os.getenv("ZMK_UPDATE_CODELOAD", "") or None
RAW_BASE = os.getenv("ZMK_UPDATE_RAW_BASE", "") or None
CONTENTS_API = os.getenv("ZMK_UPDATE_CONTENTS_API", "") or None
GITHUB_TOKEN = os.getenv("ZMK_GITHUB_TOKEN", "").strip() or None
# `git ls-remote` avoids the anonymous API quota, but a locked-down network may
# forbid git; ZMK_UPDATE_NO_GIT=1 falls back to the REST API only.
USE_GIT = os.getenv("ZMK_UPDATE_NO_GIT", "0").strip() != "1"

# --- Зеркалирование журнала в API (вкладка «Логи») ---------------------------
# Полный журнал остаётся в `docker compose logs updater`, но ход обновления
# удобнее разбирать в веб-консоли: строки уходят в /api/service-logs тем же
# сервисным токеном, что и у ботов. Без ZMK_API_URL зеркало просто выключено.
ZMK_API_URL = os.getenv("ZMK_API_URL", "").rstrip("/")
SERVICE_TOKEN_FILE = Path(os.getenv("ZMK_BOT_API_TOKEN_FILE", "/bot-secrets/.api-token"))
LOG_SHIP_SERVICE = "updater"
LOG_SHIP_BATCH = 100
LOG_SHIP_INTERVAL_SECONDS = 5.0
_log_ship_lines: deque[tuple[str, str, str]] = deque(maxlen=300)
_log_ship_lock = threading.Lock()


def ship_log(message: str, level: str = "INFO") -> None:
    """Buffer one updater event for the unified project journal."""
    text = str(message).strip()[:1800]
    if not text:
        return
    with _log_ship_lock:
        _log_ship_lines.append((datetime.now(timezone.utc).isoformat(timespec="seconds"), level, text))


class _ProjectLogHandler(logging.Handler):
    """Mirror uvicorn/library logging into the same buffer."""

    # Без фильтра каждая отправка журнала добавляла бы строку httpx и поток
    # зацикливался сам на себе; предупреждения и ошибки проходят всегда.
    SKIP_LOGGERS = frozenset({"httpx", "httpx2", "httpcore", "asyncio", "urllib3", "uvicorn.access"})

    def emit(self, record: logging.LogRecord) -> None:
        if (record.name in self.SKIP_LOGGERS or record.name.split(".", 1)[0] in self.SKIP_LOGGERS) and record.levelno < logging.WARNING:
            return
        # Ошибку emit logging сам отдаёт в handleError: обновление не прервётся.
        ship_log(self.format(record).strip(), record.levelname)


def _service_token() -> str:
    try:
        return SERVICE_TOKEN_FILE.read_text(encoding="utf-8").strip() if SERVICE_TOKEN_FILE.is_file() else ""
    except OSError:
        return ""


async def log_ship_worker() -> None:
    """Периодически отдавать накопленные строки в единый журнал проекта."""
    while True:
        await asyncio.sleep(LOG_SHIP_INTERVAL_SECONDS)
        with _log_ship_lock:
            if not _log_ship_lines:
                continue
            batch = [_log_ship_lines.popleft() for _ in range(min(LOG_SHIP_BATCH, len(_log_ship_lines)))]
        payload = {"service": LOG_SHIP_SERVICE, "entries": [{"timestamp": stamp, "level": level, "message": line} for stamp, level, line in batch]}
        try:
            headers = {"X-Bot-Service-Token": _service_token()} if _service_token() else {}
            async with httpx.AsyncClient(base_url=ZMK_API_URL, headers=headers, timeout=15) as client:
                (await client.post("/api/service-logs", json=payload)).raise_for_status()
        except (httpx.HTTPError, OSError, RuntimeError, ValueError):
            with _log_ship_lock:
                for entry in reversed(batch):
                    _log_ship_lines.appendleft(entry)


@asynccontextmanager
async def lifespan(app: FastAPI):
    handler = _ProjectLogHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root = logging.getLogger()
    if not any(isinstance(item, _ProjectLogHandler) for item in root.handlers):
        root.addHandler(handler)
    ship_task = asyncio.create_task(log_ship_worker()) if ZMK_API_URL else None
    # A plain `git pull && docker compose up -d --build` must not leave the
    # panel showing an older commit: record the tree that is really mounted.
    record_local_build(ROOT, branch=BRANCH, channel=CHANNEL)
    ship_log(
        "updater запущен "
        f"(root={ROOT}, repo={REPO}, channel={CHANNEL}, branch={BRANCH}, current={local_state(ROOT)['display']})"
    )
    try:
        yield
    finally:
        if ship_task is not None and not ship_task.done():
            ship_task.cancel()
            await asyncio.gather(ship_task, return_exceptions=True)
        root.removeHandler(handler)


app = FastAPI(title="Zovod Updater", version="2.12.0", lifespan=lifespan)


def _require_token(x_update_token: str | None) -> None:
    if TOKEN and x_update_token != TOKEN:
        raise HTTPException(403, "invalid updater token")


class ApplyResponse(BaseModel):
    status: str
    message: str
    result: dict[str, Any] | None = None


def _update_kwargs(*, apply: bool = False) -> dict[str, Any]:
    """Settings shared by the status check and the update itself.

    ``contents_api`` is only used when applying an update: it pins the
    downloaded tree to the commit that was checked.
    """
    kwargs: dict[str, Any] = {
        "channel": CHANNEL,
        "repo": REPO,
        "branch": BRANCH,
        "api_url": API_URL,
        "dl_base": DL_BASE,
        "commits_api": COMMITS_API,
        "compare_api": COMPARE_API,
        "codeload": CODELOAD,
        "raw_base": RAW_BASE,
        "token": GITHUB_TOKEN,
        "use_git": USE_GIT,
        # Per-request budget for the head-commit lookup (git ls-remote, the
        # REST API, the raw VERSION file, the compare API). The panel proxies
        # /status with a 30 s timeout, so the check always answers in time.
        "timeout": 6.0,
    }
    if apply:
        kwargs["contents_api"] = CONTENTS_API
    return kwargs


@app.get("/health")
def health() -> dict[str, Any]:
    state = local_state(ROOT)
    return {"status": "ok", "root": str(ROOT), "current": state["display"], "version": state["version"], "commit": state["commit"]}


@app.get("/status")
def status(x_update_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_token(x_update_token)
    data = plan_update(ROOT, **_update_kwargs())
    data["root"] = str(ROOT)
    return data


@app.post("/apply", response_model=ApplyResponse)
def apply(x_update_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_token(x_update_token)
    ship_log(f"update requested from the web panel (channel={CHANNEL}, branch={BRANCH})")
    try:
        result = apply_update(ROOT, **_update_kwargs(apply=True))
    except UpdateError as exc:
        ship_log(f"update failed: {exc}", "ERROR")
        raise HTTPException(400, str(exc)) from exc
    if result.get("applied"):
        # Kick a detached redeploy so the running stack picks up the new code.
        _start_redploy()
        ship_log(
            f"update applied: {result.get('current')} -> {result.get('latest')} "
            f"(commit {str(result.get('commit') or '')[:7]}), redeploy started"
        )
        return ApplyResponse(status="updated", message="Обновление применено, сервисы перезапускаются.", result=result)
    ship_log(f"update skipped: already on {result.get('latest') or result.get('current')}")
    return ApplyResponse(status="up_to_date", message="Уже установлена последняя версия.", result=result)


def _start_redploy() -> None:
    """Re-run docker compose (via the mounted socket) in the background."""
    profiles: list[str] = []
    profiles_file = ROOT / ".zmk-profiles"
    if profiles_file.is_file():
        for line in profiles_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("--profile"):
                profiles.append(line)

    runtimes = _docker_runtimes()
    env = dict(os.environ)
    if "nvidia" in runtimes:
        env["COMPOSE_FILE"] = "docker-compose.yml:docker-compose.gpu.yml"

    log = ROOT / "update-redploy.log"
    with log.open("w", encoding="utf-8") as fh:
        # Deliberately run in a new session so it survives this container.
        docker_cli = shutil.which("docker") or "docker"
        subprocess.Popen(  # nosec B603 B607  (static argv list, no shell, no user input)
            [docker_cli, *profiles, "compose", "up", "-d", "--build", "--remove-orphans"],
            cwd=str(ROOT),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _docker_runtimes() -> str:
    try:
        docker_cli = shutil.which("docker") or "docker"
        out = subprocess.run(  # nosec B603 B607  (static argv list, no shell, no user input)
            [docker_cli, "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            timeout=10,
            text=True,
            check=False,
        ).stdout
        return out.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8020, log_level="info")  # nosec B104  (container binding)
