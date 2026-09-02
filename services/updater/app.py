"""ZMK Vision updater sidecar.

This service runs inside the Docker network with the host project root
bind-mounted at UPDATE_ROOT (default /workspace) and the Docker socket at
/var/run/docker.sock. It performs real, verifiable updates: it fetches the
latest release, verifies the SHA256 checksum, swaps the new files into the
host project directory (preserving .env, ./data, databases and the saved
Compose profiles) and then redeploys the application containers so the new
code actually runs.

The Web panel reaches this service through the backend, which proxies
GET /api/update/status and POST /api/update/apply here.
"""
from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404  (controlled Docker CLI, invoked without shell)
from pathlib import Path
from typing import Any

from core import UpdateError, apply_update, current_version, plan_update
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

ROOT = Path(os.getenv("UPDATE_ROOT", "/workspace"))
REPO = os.getenv("ZMK_UPDATE_REPO", "danilka-revin/zmk-videoanalytics")
TOKEN = os.getenv("ZMK_UPDATE_TOKEN", "").strip()
API_URL = os.getenv("ZMK_UPDATE_API", "") or None
DL_BASE = os.getenv("ZMK_UPDATE_DL_BASE", "") or None

app = FastAPI(title="ZMK Vision Updater", version="2.12.0")


def _require_token(x_update_token: str | None) -> None:
    if TOKEN and x_update_token != TOKEN:
        raise HTTPException(403, "invalid updater token")


class ApplyResponse(BaseModel):
    status: str
    message: str
    result: dict[str, Any] | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "root": str(ROOT), "current": current_version(ROOT)}


@app.get("/status")
def status(x_update_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_token(x_update_token)
    data = plan_update(ROOT, repo=REPO, api_url=API_URL, dl_base=DL_BASE)
    data["root"] = str(ROOT)
    return data


@app.post("/apply", response_model=ApplyResponse)
def apply(x_update_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_token(x_update_token)
    try:
        result = apply_update(ROOT, repo=REPO, api_url=API_URL, dl_base=DL_BASE)
    except UpdateError as exc:
        raise HTTPException(400, str(exc)) from exc
    if result.get("applied"):
        # Kick a detached redeploy so the running stack picks up the new code.
        _start_redploy()
        return ApplyResponse(status="updated", message="Обновление применено, сервисы перезапускаются.", result=result)
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
