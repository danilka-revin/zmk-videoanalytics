import time
import uuid
from fastapi.testclient import TestClient
from app.main import app


def test_error_report_and_csv():
    with TestClient(app) as c:
        created = c.post('/api/admin/logs/simulate-error')
        assert created.status_code == 201
        report = c.get('/api/reports/errors?hours=24')
        assert report.status_code == 200
        assert set(report.json()['summary']) == {'WARNING', 'ERROR', 'CRITICAL'}
        csv = c.get('/api/reports/errors.csv?hours=24')
        assert csv.status_code == 200
        assert 'text/csv' in csv.headers['content-type']


def test_hot_swap_is_atomic_and_audited():
    with TestClient(app) as c:
        result = c.post('/api/models/siz-guard-v2.0/activate')
        assert result.status_code == 200
        assert result.json()['hot_swap'] is True
        assert result.json()['downtime_ms'] == 0
        models = c.get('/api/models').json()
        assert next(x for x in models if x['name'] == 'siz-guard-v2.0')['active'] is True
        assert c.post('/api/models/missing/activate').status_code == 404


def test_training_rejects_offline_camera_and_completes_online_job():
    with TestClient(app) as c:
        assert c.post('/api/training/jobs', json={'camera_id':'cam_07'}).status_code == 409
        target_name = f"test-camera-{uuid.uuid4().hex[:10]}"
        started = c.post('/api/training/jobs', json={'camera_id':'cam_01','image_count':20,'epochs':1,'target_name':target_name})
        assert started.status_code == 202
        job_id = started.json()['id']
        deadline = time.time() + 8
        job = {}
        while time.time() < deadline:
            job = c.get(f'/api/training/jobs/{job_id}').json()
            if job['status'] == 'completed':
                break
            time.sleep(.25)
        assert job['status'] == 'completed'
        assert job['progress'] == 100
        models = c.get('/api/models').json()
        assert any(x['name'] == target_name and x['status'] == 'ready' for x in models)
