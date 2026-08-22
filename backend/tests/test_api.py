from app.main import app
from fastapi.testclient import TestClient


def test_health_and_dashboard():
    with TestClient(app) as c:
        assert c.get('/api/health').status_code == 200
        d=c.get('/api/dashboard').json()
        assert d['cameras']['total'] == 10
        assert d['messenger_provider'] in {'none','telegram','max'}

def test_ack_existing_event():
    with TestClient(app) as c:
        event=c.get('/api/events?limit=1').json()[0]
        assert c.post(f"/api/events/{event['id']}/ack",json={'note':'Проверено'}).status_code == 200


def test_capabilities_expose_inference_worker_flag():
    with TestClient(app) as c:
        cap=c.get('/api/capabilities').json()
        assert 'inference_worker' in cap
        assert isinstance(cap['inference_worker'], bool)
        assert 'fresh_snapshots' in cap
