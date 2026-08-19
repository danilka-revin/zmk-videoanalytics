from app.main import app
from fastapi.testclient import TestClient


def test_health_and_dashboard():
    with TestClient(app) as c:
        assert c.get('/api/health').status_code == 200
        d=c.get('/api/dashboard').json()
        assert d['cameras']['total'] == 10
        assert d['messenger_provider'] in {'none','telegram','max'}

def test_simulate_and_ack():
    with TestClient(app) as c:
        e=c.post('/api/events/simulate').json()
        assert c.post(f"/api/events/{e['id']}/ack",json={'note':'Проверено'}).status_code == 200
