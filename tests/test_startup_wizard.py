"""start.sh first-run wizard: configures .env + .zmk-profiles before asking
for docker, so a fresh clone can be started with a single command."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sandbox(tmp_path):
    """A minimal project copy (no git) for running start.sh."""
    d = tmp_path / "repo"
    d.mkdir()
    for name in [".env.example", "docker-compose.yml", "start.sh"]:
        shutil.copy(ROOT / name, d / name)
    (d / "installers").mkdir()
    for name in ["wizard.sh", "auto-update.sh", "install-linux.sh"]:
        shutil.copy(ROOT / "installers" / name, d / "installers" / name)
    (d / "backend").mkdir(); shutil.copy(ROOT / "backend" / "Dockerfile", d / "backend" / "Dockerfile")
    (d / "frontend").mkdir(); shutil.copy(ROOT / "frontend" / "Dockerfile", d / "frontend" / "Dockerfile")
    return d


def test_first_run_runs_wizard_even_without_docker(sandbox):
    # No docker in this env; the wizard must still configure before failing on
    # the docker check.
    env = {"NONINTERACTIVE": "1", "MESSENGER_PROVIDER": "none", "ZMK_NO_AUTO_UPDATE": "1"}
    r = subprocess.run(["bash", "start.sh"], cwd=sandbox, env={**__import__("os").environ, **env},
                       text=True, capture_output=True, check=False)
    # Wizard message appears and .env / .zmk-profiles get written.
    assert "Конфигурация сохранена" in r.stdout
    assert (sandbox / ".zmk-profiles").exists()
    envtxt = (sandbox / ".env").read_text()
    assert "MESSENGER_PROVIDER=none" in envtxt
    # Then it stopped at the docker check (no docker present in this env).
    assert "Docker" in (r.stdout + r.stderr)
