import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_linux_installers_pass_shell_parser_and_dry_check():
    scripts = [ROOT/'installers/bootstrap-linux.sh', ROOT/'installers/install-linux.sh', ROOT/'installers/uninstall-linux.sh', ROOT/'installers/wizard.sh', ROOT/'start.sh']
    subprocess.run(['bash','-n',*[str(x) for x in scripts]], check=True)
    result = subprocess.run(['bash',str(ROOT/'installers/install-linux.sh'),'--check'], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert 'Installer validation: OK' in result.stdout
    bootstrap = subprocess.run(['bash',str(ROOT/'installers/bootstrap-linux.sh'),'--check'], cwd=ROOT, text=True, capture_output=True, check=False)
    assert bootstrap.returncode == 0, bootstrap.stderr
    assert 'bootstrap launcher: OK' in bootstrap.stdout
    # The config wizard lives in its own shared file sourced by both the
    # installer and start.sh, so the messenger/token/profile logic is there.
    wizard = (ROOT/'installers/wizard.sh').read_text()
    for needle in ['MESSENGER_PROVIDER', 'MAX_BOT_TOKEN', 'Admin → Боты', 'run_config']:
        assert needle in wizard
    assert 'set_env MAX_BOT_TOKEN ""' not in wizard
    assert 'set_env TELEGRAM_BOT_TOKEN ""' not in wizard
    # start.sh must source the wizard and provide --setup / first-run path.
    start = (ROOT/'start.sh').read_text()
    assert 'installers/wizard.sh' in start and 'run_config' in start and '--setup' in start
    assert '.zmk-profiles' in start
    # The one-command launcher retries a known BuildKit snapshot-cache failure
    # without pruning persistent project volumes.
    for script in (start, (ROOT/'installers/install-linux.sh').read_text()):
        assert 'builder prune -af' in script
        assert 'COMPOSE_PARALLEL_LIMIT=1' in script
        assert 'buildx prune -af' in script
        assert 'ZMK VISION' in script
        assert '███████' in script
        assert 'VIDEO ANALYTICS CONTROL PLATFORM' in script
        assert 'SERVICE STATUS' in script
        assert 'API HEALTH CHECK' in script


def test_bootstrap_launcher_and_rtsp_wizard_escape_credentials(tmp_path):
    bootstrap = (ROOT/'installers/bootstrap-linux.sh').read_text()
    for required in ['git clone', 'ZMK_REF', 'ZMK_INSTALL_DIR', 'zmk-vision', '/dev/tty', 'NONINTERACTIVE=1', 'ENABLE_INFERENCE=true']:
        assert required in bootstrap
    # Explicit shallow fetches populate FETCH_HEAD, but do not guarantee a
    # remote-tracking origin/<slash-containing-branch> ref. The repeat launcher
    # must therefore check out the fetched commit directly.
    assert 'checkout -B "$ZMK_REF" FETCH_HEAD' in bootstrap
    # bootstrap has already checked out the requested Git ref; the release
    # updater must not replace a feature branch immediately afterwards.
    assert 'ZMK_NO_AUTO_UPDATE=1' in bootstrap

    # An RTSP password may include & or |. The wizard must preserve it rather
    # than interpreting it as a sed replacement expression.
    (tmp_path/'.env').write_text((ROOT/'.env.example').read_text())
    value = 'rtsp://admin:p&ss|word!@192.0.2.10:554/stream'
    result = subprocess.run(
        ['bash', '-c', 'source "$1"; set_env RTSP_CAM_01 "$VALUE"; grep "^RTSP_CAM_01=" .env', '_', str(ROOT/'installers/wizard.sh')],
        cwd=tmp_path,
        env={**__import__('os').environ, 'VALUE': value},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == f'RTSP_CAM_01={value}'


def test_shallow_fetch_of_feature_branch_checks_out_fetch_head(tmp_path):
    """Git only sets FETCH_HEAD for an explicit shallow branch fetch.

    This reproduces the one-command updater's branch-switch path without a
    network dependency.  `origin/arena/...` is intentionally absent, while
    `checkout -B <branch> FETCH_HEAD` succeeds.
    """
    def git(*args, cwd):
        return subprocess.run(['git', *args], cwd=cwd, text=True, capture_output=True, check=True)

    remote = tmp_path / 'remote.git'
    source = tmp_path / 'source'
    clone = tmp_path / 'clone'
    git('init', '--bare', str(remote), cwd=tmp_path)
    git('init', '-b', 'main', str(source), cwd=tmp_path)
    git('config', 'user.email', 'test@example.invalid', cwd=source)
    git('config', 'user.name', 'Test', cwd=source)
    (source / 'version.txt').write_text('main\n')
    git('add', '.', cwd=source); git('commit', '-m', 'main', cwd=source)
    git('remote', 'add', 'origin', str(remote), cwd=source)
    git('push', '-u', 'origin', 'main', cwd=source)
    git('checkout', '-b', 'arena/test-launcher', cwd=source)
    (source / 'version.txt').write_text('feature\n')
    git('commit', '-am', 'feature', cwd=source)
    expected = git('rev-parse', 'HEAD', cwd=source).stdout.strip()
    git('push', 'origin', 'arena/test-launcher', cwd=source)

    git('clone', '--depth=1', '--branch', 'main', f'file://{remote}', str(clone), cwd=tmp_path)
    git('fetch', '--depth=1', 'origin', 'arena/test-launcher', cwd=clone)
    missing_tracking = subprocess.run(
        ['git', 'show-ref', '--verify', '--quiet', 'refs/remotes/origin/arena/test-launcher'],
        cwd=clone,
        check=False,
    )
    assert missing_tracking.returncode != 0
    git('checkout', '-B', 'arena/test-launcher', 'FETCH_HEAD', cwd=clone)
    assert git('rev-parse', 'HEAD', cwd=clone).stdout.strip() == expected


def test_windows_wrapper_and_powershell_structure():
    bat = (ROOT/'installers/install-windows.bat').read_text()
    ps = (ROOT/'installers/install-windows.ps1').read_text()
    assert 'install-windows.ps1' in bat and '%*' in bat
    for required in ['param(', 'Assert-ProjectFiles', 'docker compose', 'Wait-Http', 'config --quiet', '--remove-orphans', 'MESSENGER_PROVIDER', 'MAX_BOT_TOKEN', 'TRAINING_WORKER_URL']:
        assert required in ps
    # Lightweight delimiter guard for environments where pwsh is unavailable.
    clean = re.sub(r'(?s)<#.*?#>|#.*', '', ps)
    clean = re.sub(r'"(?:`.|[^"`])*"|\'(?:\'\'|[^\'])*\'', '', clean)
    pairs = {'{':'}','(':')','[':']'}; stack=[]
    for char in clean:
        if char in pairs: stack.append(pairs[char])
        elif char in pairs.values():
            assert stack and stack.pop() == char
    assert not stack


def test_compose_and_environment_are_consistent():
    compose = yaml.safe_load((ROOT/'docker-compose.yml').read_text())
    gpu_override = yaml.safe_load((ROOT/'docker-compose.gpu.yml').read_text())
    assert gpu_override['services']['training-worker']['gpus'] == 'all'
    assert compose['services']['web']['build'] == './frontend'
    assert 'healthcheck' in compose['services']['api']
    assert compose['services']['api']['environment']['VIDEOANALYTICS_DB'] == '/app/data/videoanalytics.db'
    assert compose['services']['api']['environment']['MODEL_UPLOAD_MAX_BYTES'] == '${MODEL_UPLOAD_MAX_BYTES:-2000000000}'
    assert compose['services']['api']['ports'] == ['127.0.0.1:8000:8000']
    # Both messenger workers stay available and idle safely until the Admin → Боты
    # control plane enables a provider; this makes UI toggles real without Docker CLI use.
    assert 'profiles' not in compose['services']['telegram-bot']
    assert 'profiles' not in compose['services']['max-bot']
    assert compose['services']['telegram-bot']['restart'] == 'unless-stopped'
    assert compose['services']['max-bot']['restart'] == 'unless-stopped'
    assert compose['services']['max-bot']['build'] == './services/max_bot'
    assert compose['services']['api']['environment']['MAX_BOT_TOKEN'] == '${MAX_BOT_TOKEN:-}'
    # Admin-entered bot tokens stay in a dedicated API-writable volume, never
    # a broad read-only mount of the database/RTSP data into bot workers.
    assert compose['services']['api']['environment']['ZMK_BOT_TOKEN_DIR'] == '/bot-tokens'
    assert 'bot-token-data:/bot-tokens' in compose['services']['api']['volumes']
    for service in ('telegram-bot', 'max-bot'):
        assert compose['services'][service]['environment']['ZMK_BOT_TOKEN_DIR'] == '/bot-secrets'
        assert 'bot-token-data:/bot-secrets:ro' in compose['services'][service]['volumes']
    assert 'bot-token-data' in compose['volumes']
    assert 'profiles' not in compose['services']['training-worker']
    assert compose['services']['training-worker']['build'] == './services/training_worker'
    assert compose['services']['api']['environment']['TRAINING_WORKER_URL'] == '${TRAINING_WORKER_URL:-http://training-worker:8010}'
    assert compose['services']['inference-worker']['profiles'] == ['inference']
    assert compose['services']['inference-worker']['build'] == './services/inference_worker'
    assert 'RUN nginx -t' in (ROOT/'frontend/Dockerfile').read_text()
    nginx = (ROOT/'frontend/nginx.conf').read_text()
    assert 'set $api_upstream http://api:8000' in nginx and 'proxy_pass $api_upstream' in nginx
    assert "img-src 'self' data: blob:" in nginx
    assert '/mjpeg$' in nginx and 'proxy_buffering off' in nginx
    assert 'location ^~ /telegram' in nginx and 'https://web.telegram.org' in nginx
    assert 'location = /api/models/upload' in nginx and 'client_max_body_size 2g' in nginx and 'proxy_request_buffering off' in nginx
    lines = [x for x in (ROOT/'.env.example').read_text().splitlines() if x and not x.startswith('#')]
    keys = [x.split('=',1)[0] for x in lines]
    assert len(keys) == len(set(keys))
    assert {'MESSENGER_PROVIDER','ZMK_API_KEY','TELEGRAM_BOT_TOKEN','MAX_BOT_TOKEN','MAX_ADMIN_IDS','MODEL_UPLOAD_MAX_BYTES','POSTGRES_PASSWORD','MINIO_ROOT_PASSWORD'} <= set(keys)


def test_release_contains_installation_assets():
    workflow = (ROOT/'.github/workflows/release.yml').read_text()
    for path in ['installers/install-windows.ps1','installers/install-windows.bat','installers/install-linux.sh']:
        assert (ROOT/path).exists()
        assert path in workflow
