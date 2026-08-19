from datetime import datetime, timedelta, timezone
import uuid
from fastapi.testclient import TestClient
from app.main import app, db, now_iso


def current_model(c):
    return next(x['name'] for x in c.get('/api/models').json() if x['active'])


def test_all_event_thresholds_and_detection_idempotency():
    with TestClient(app) as c:
        model = current_model(c)
        settings=c.get('/api/settings').json()
        assert {'helmet_conf','vest_conf','phone_conf','smoking_conf','restricted_zone_conf','immobility_conf'} <= set(settings)
        c.put('/api/settings/smoking_conf', json={'value':.90})
        low = c.post('/api/inference/detections', json={'detections':[{
            'camera_id':'cam_01','model_name':model,'event_type':'smoking','confidence':.89,
            'detection_id':f'low-{uuid.uuid4().hex}'
        }]}).json()
        assert low['rejected'][0]['reason'] == 'below_threshold:0.9'
        detection_id = f'det-{uuid.uuid4().hex}'
        payload = {'detections':[{
            'camera_id':'cam_01','model_name':model,'event_type':'smoking','confidence':.95,
            'detection_id':detection_id,'bbox':[10,20,110,220]
        }]}
        first = c.post('/api/inference/detections', json=payload).json()
        second = c.post('/api/inference/detections', json=payload).json()
        assert first['accepted'][0]['event_id'] == second['accepted'][0]['event_id']
        assert second['accepted'][0]['duplicate'] is True
        c.put('/api/settings/smoking_conf', json={'value':.80})


def test_detection_geometry_and_time_window():
    with TestClient(app) as c:
        model = current_model(c)
        bad_box = c.post('/api/inference/detections', json={'detections':[{
            'camera_id':'cam_01','model_name':model,'event_type':'no_helmet','confidence':.99,
            'bbox':[100,100,50,50]
        }]})
        assert bad_box.status_code == 422
        old = (datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
        result = c.post('/api/inference/detections', json={'detections':[{
            'camera_id':'cam_01','model_name':model,'event_type':'no_helmet','confidence':.99,
            'timestamp':old
        }]}).json()
        assert result['rejected'][0]['reason'] == 'timestamp_out_of_range'


def test_model_activation_is_idempotent_and_health_is_consistent():
    with TestClient(app) as c:
        model = current_model(c)
        result = c.post(f'/api/models/{model}/activate')
        assert result.status_code == 200
        assert result.json()['idempotent'] is True
        assert result.json()['hot_swap'] is False
        health = c.get('/api/models/active/health')
        assert health.status_code == 200
        assert health.json()['healthy'] is True
        assert health.json()['model']['name'] == model
        assert health.json()['requirements'] == {'precision':90.0,'recall':85.0}


def test_model_below_quality_gate_cannot_be_activated():
    name=f'low-quality-{uuid.uuid4().hex[:8]}'
    with TestClient(app) as c:
        con=db(); con.execute("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source) VALUES(?,?,?,?,?,?,?)",(name,'ONNX','ready',50,40,now_iso(),'test')); con.commit(); con.close()
        result=c.post(f'/api/models/{name}/activate')
        assert result.status_code == 409
        assert 'ниже' in result.json()['detail']


def test_training_has_single_job_guard_and_can_be_cancelled():
    with TestClient(app) as c:
        target = f'cancel-test-{uuid.uuid4().hex[:10]}'
        started = c.post('/api/training/jobs', json={
            'camera_id':'cam_01','image_count':20,'epochs':1,'target_name':target
        })
        assert started.status_code == 202
        job_id = started.json()['id']
        second = c.post('/api/training/jobs', json={
            'camera_id':'cam_02','image_count':20,'epochs':1,
            'target_name':f'parallel-{uuid.uuid4().hex[:10]}'
        })
        assert second.status_code == 409
        cancelled = c.post(f'/api/training/jobs/{job_id}/cancel')
        assert cancelled.status_code == 200
        assert c.get(f'/api/training/jobs/{job_id}').json()['status'] == 'cancelled'
        assert c.post(f'/api/training/jobs/{job_id}/cancel').status_code == 409
