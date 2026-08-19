import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
from fastapi.testclient import TestClient
import app.main as main


def test_security_headers_and_cache_policy():
    with TestClient(main.app) as c:
        r = c.get('/api/health')
        assert r.status_code == 200
        assert r.headers['x-content-type-options'] == 'nosniff'
        assert r.headers['x-frame-options'] == 'SAMEORIGIN'
        assert r.headers['cache-control'] == 'no-store'


def test_optional_api_key_protects_non_public_api():
    previous = main.API_KEY
    main.API_KEY = 'test-secret-key'
    try:
        with TestClient(main.app) as c:
            assert c.get('/api/health').status_code == 200
            assert c.get('/api/dashboard').status_code == 401
            assert c.get('/api/dashboard', headers={'X-API-Key':'wrong'}).status_code == 401
            assert c.get('/api/dashboard', headers={'X-API-Key':'test-secret-key'}).status_code == 200
    finally:
        main.API_KEY = previous


def telegram_init_data(token: str, user_id: int) -> str:
    values = {'auth_date':str(int(time.time())), 'query_id':'test-query', 'user':json.dumps({'id':user_id}, separators=(',',':'))}
    check = '\n'.join(f'{k}={v}' for k,v in sorted(values.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    values['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_telegram_mini_app_signature_and_role_permissions():
    old_key, old_token, old_roles = main.API_KEY, main.TELEGRAM_BOT_TOKEN, main.TELEGRAM_ROLES
    main.API_KEY = 'protected-api'; main.TELEGRAM_BOT_TOKEN = '123456:test-token'; main.TELEGRAM_ROLES = {100:'admin', 200:'viewer'}
    try:
        with TestClient(main.app) as c:
            admin = {'X-Telegram-Init-Data':telegram_init_data(main.TELEGRAM_BOT_TOKEN,100)}
            viewer = {'X-Telegram-Init-Data':telegram_init_data(main.TELEGRAM_BOT_TOKEN,200)}
            assert c.get('/api/dashboard', headers=admin).status_code == 200
            assert c.get('/api/dashboard', headers=viewer).status_code == 200
            assert c.get('/api/admin/config', headers=viewer).status_code == 403
            assert c.get('/api/dashboard', headers={'X-Telegram-Init-Data':'broken'}).status_code == 401
    finally:
        main.API_KEY, main.TELEGRAM_BOT_TOKEN, main.TELEGRAM_ROLES = old_key, old_token, old_roles


def test_request_size_limit():
    with TestClient(main.app) as c:
        r = c.post('/api/inference/detections', headers={'content-length':'2000001'}, content=b'{}')
        assert r.status_code == 413
