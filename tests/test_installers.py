from pathlib import Path
import re
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_linux_installers_pass_shell_parser_and_dry_check():
    scripts = [ROOT/'installers/install-linux.sh', ROOT/'installers/uninstall-linux.sh']
    subprocess.run(['bash','-n',*[str(x) for x in scripts]], check=True)
    result = subprocess.run(['bash',str(scripts[0]),'--check'], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'Installer validation: OK' in result.stdout


def test_windows_wrapper_and_powershell_structure():
    bat = (ROOT/'installers/install-windows.bat').read_text()
    ps = (ROOT/'installers/install-windows.ps1').read_text()
    assert 'install-windows.ps1' in bat and '%*' in bat
    for required in ['param(', 'Assert-ProjectFiles', 'docker compose', 'Wait-Http', 'config --quiet', '--remove-orphans']:
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
    assert compose['services']['web']['build'] == './frontend'
    assert 'healthcheck' in compose['services']['api']
    assert compose['services']['api']['environment']['VIDEOANALYTICS_DB'] == '/app/data/videoanalytics.db'
    nginx = (ROOT/'frontend/nginx.conf').read_text()
    assert 'proxy_pass http://api:8000' in nginx
    assert 'location ^~ /telegram' in nginx and 'https://web.telegram.org' in nginx
    lines = [x for x in (ROOT/'.env.example').read_text().splitlines() if x and not x.startswith('#')]
    keys = [x.split('=',1)[0] for x in lines]
    assert len(keys) == len(set(keys))
    assert {'ZMK_API_KEY','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','MINIO_ROOT_PASSWORD'} <= set(keys)


def test_release_contains_installation_assets():
    workflow = (ROOT/'.github/workflows/release.yml').read_text()
    for path in ['installers/install-windows.ps1','installers/install-windows.bat','installers/install-linux.sh']:
        assert (ROOT/path).exists()
        assert path in workflow
