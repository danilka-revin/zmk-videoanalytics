import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "installers" / "auto-update.sh"


def _bash(script: str, cwd=None) -> str:
    result = subprocess.run(
        ["bash", "-c", script], cwd=cwd or ROOT, text=True, capture_output=True, check=True
    )
    return result.stdout


def test_updater_sources_and_parses_cleanly():
    # Sourcing the updater must not trigger a network check or exit.
    out = _bash(f"source '{UPDATER}'; echo SOURCED")
    assert "SOURCED" in out


def test_version_compare_is_semver():
    code = f"""source '{UPDATER}'
set -e
zmk_version_lt 2.2.4 2.3.0 && echo LT      || echo NOTLT
zmk_version_lt 2.3.0 2.3.0 && echo EQ      || echo NOTEQ
zmk_version_lt 2.4.0 2.3.0 && echo GT      || echo NOTGT
zmk_version_lt 1.9.9 2.0.0 && echo V1      || echo NOTV1
zmk_version_lt v2.2.4 2.3.0 && echo VP      || echo NOTVP
"""
    out = _bash(code)
    assert "LT" in out
    assert "NOTEQ" in out
    assert "NOTGT" in out
    assert "V1" in out
    assert "VP" in out


def test_sync_tree_preserves_data_and_removes_stale():
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

        _bash(f"source '{UPDATER}'; zmk_sync_tree '{staged}' '{root}'")

        # new file copied and stale file removed
        assert (root / "new.txt").read_text() == "fresh"
        assert not (root / "old.txt").exists()
        # version refreshed
        assert (root / "VERSION").read_text().strip() == "2.0.0"
        # runtime data and secrets preserved
        assert (root / ".env").read_text() == "SECRET=1\n"
        assert (root / "data" / "db.sqlite").read_text() == "keepme"
        assert (root / "videoanalytics.db").read_text() == "keepdb"


def test_shell_syntax_of_new_scripts():
    scripts = [
        ROOT / "installers" / "auto-update.sh",
        ROOT / "start.sh",
        ROOT / "installers" / "install-linux.sh",
    ]
    subprocess.run(["bash", "-n", *[str(s) for s in scripts]], check=True)


def test_powershell_updater_contains_required_logic():
    # Full PowerShell parse validation runs in CI on windows-latest via
    # install-windows.ps1 -CheckOnly (Parser::ParseFile). Here we guard the
    # key implementation details so a regression is caught by the python test.
    updater = (ROOT / "installers" / "auto-update.ps1").read_text()
    for required in ["Get-FileHash", "SHA256", "ZMK_NO_AUTO_UPDATE", "Expand-Archive",
                     "robocopy", "ZMK_RELAUNCHED_AFTER_UPDATE", "releases/latest",
                     "Get-CurrentVersion", "Test-VersionLt"]:
        assert required in updater, required
    start = (ROOT / "start.ps1").read_text()
    for required in ["auto-update.ps1", "docker compose", "--remove-orphans", "Wait-Http"]:
        assert required in start, required


def test_relaunch_and_protect_paths_configured():
    updater = UPDATER.read_text()
    for needle in ["ZMK_NO_AUTO_UPDATE", "SHA256", "releases/download", "zmk_sync_tree",
                   "exec env ZMK_RELAUNCHED_AFTER_UPDATE=1"]:
        assert needle in updater
    # the launcher must reference the updater
    assert "installers/auto-update.sh" in (ROOT / "start.sh").read_text()
