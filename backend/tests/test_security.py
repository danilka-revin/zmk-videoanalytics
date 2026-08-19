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


def test_request_size_limit():
    with TestClient(main.app) as c:
        r = c.post('/api/inference/detections', headers={'content-length':'2000001'}, content=b'{}')
        assert r.status_code == 413
