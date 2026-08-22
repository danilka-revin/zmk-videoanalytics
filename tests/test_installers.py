import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_linux_installers_pass_shell_parser_and_dry_check():
    scripts = [ROOT/'installers/install-linux.sh', ROOT/'installers/uninstall-linux.sh', ROOT/'installers/wizard.sh', ROOT/'start.sh']
    subprocess.run(['bash','-n',*[str(x) for x in scripts]], check=True)
    result = subprocess.run(['bash',str(scripts[0]),'--check'], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert 'Installer validation: OK' in result.stdout
    # The config wizard lives in its own shared file sourced by both the
    # installer and start.sh, so the messenger/token/profile logic is there.
    wizard = (ROOT/'installers/wizard.sh').read_text()
    for needle in ['MESSENGER_PROVIDER', 'MAX_BOT_TOKEN', '--profile max', '--profile telegram', 'run_config']:
        assert needle in wizard
    # start.sh must source the wizard and provide --setup / first-run path.
    start = (ROOT/'start.sh').read_text()
    assert 'installers/wizard.sh' in start and 'run_config' in start and '--setup' in start
    assert '.zmk-profiles' in start


def test_windows_wrapper_and_powershell_structure():
    bat = (ROOT/'installers/install-windows.bat').read_text()
    ps = (ROOT/'installers/install-windows.ps1').read_text()
    assert 'install-windows.ps1' in bat and '%*' in bat
    for required in ['param(', 'Assert-ProjectFiles', 'docker compose', 'Wait-Http', 'config --quiet', '--remove-orphans', 'MESSENGER_PROVIDER', 'MAX_BOT_TOKEN', 'ENABLE_TRAINING', 'TRAINING_WORKER_URL']:
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
    assert compose['services']['api']['ports'] == ['127.0.0.1:8000:8000']
    assert compose['services']['telegram-bot']['profiles'] == ['telegram']
    assert compose['services']['max-bot']['profiles'] == ['max']
    assert compose['services']['max-bot']['build'] == './services/max_bot'
    assert compose['services']['training-worker']['profiles'] == ['training']
    assert compose['services']['training-worker']['build'] == './services/training_worker'
    assert compose['services']['inference-worker']['profiles'] == ['inference']
    assert compose['services']['inference-worker']['build'] == './services/inference_worker'
    assert 'RUN nginx -t' in (ROOT/'frontend/Dockerfile').read_text()
    nginx = (ROOT/'frontend/nginx.conf').read_text()
    assert 'set $api_upstream http://api:8000' in nginx and 'proxy_pass $api_upstream' in nginx
    assert 'location ^~ /telegram' in nginx and 'https://web.telegram.org' in nginx
    lines = [x for x in (ROOT/'.env.example').read_text().splitlines() if x and not x.startswith('#')]
    keys = [x.split('=',1)[0] for x in lines]
    assert len(keys) == len(set(keys))
    assert {'MESSENGER_PROVIDER','ZMK_API_KEY','TELEGRAM_BOT_TOKEN','MAX_BOT_TOKEN','MAX_ADMIN_IDS','POSTGRES_PASSWORD','MINIO_ROOT_PASSWORD'} <= set(keys)


def test_release_contains_installation_assets():
    workflow = (ROOT/'.github/workflows/release.yml').read_text()
    for path in ['installers/install-windows.ps1','installers/install-windows.bat','installers/install-linux.sh']:
        assert (ROOT/path).exists()
        assert path in workflow
