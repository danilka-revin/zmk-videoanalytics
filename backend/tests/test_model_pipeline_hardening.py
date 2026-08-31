import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app import main as app_main
from app.main import app, db, now_iso
from fastapi.testclient import TestClient


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


def test_event_cooldown_suppresses_frame_by_frame_alert_flood():
    with TestClient(app) as c:
        model=current_model(c); person=f'COOLDOWN-{uuid.uuid4().hex[:8]}'
        base={'camera_id':'cam_01','model_name':model,'event_type':'no_helmet','confidence':.99,'person_id':person}
        first=c.post('/api/inference/detections',json={'detections':[{**base,'detection_id':f'det-{uuid.uuid4().hex}'}]}).json()
        second=c.post('/api/inference/detections',json={'detections':[{**base,'detection_id':f'det-{uuid.uuid4().hex}'}]}).json()
        assert len(first['accepted'])==1
        assert second['rejected'][0]['reason'].startswith('event_cooldown:')
        assert second['rejected'][0]['event_id']==first['accepted'][0]['event_id']


def test_detection_timestamps_are_normalized_to_site_timezone():
    with TestClient(app) as c:
        model=current_model(c); detection_id=f'tz-{uuid.uuid4().hex}'
        utc_time=datetime.now(timezone.utc).isoformat()
        result=c.post('/api/inference/detections',json={'detections':[{'camera_id':'cam_01','model_name':model,'event_type':'immobility','confidence':.99,'timestamp':utc_time,'detection_id':detection_id,'person_id':detection_id}]})
        assert result.status_code==200 and result.json()['accepted']
        con=db(); timestamp=con.execute("SELECT timestamp FROM events WHERE external_id=?",(detection_id,)).fetchone()[0]; con.close()
        assert timestamp.endswith('+07:00')


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
        dashboard=c.get('/api/dashboard').json()
        assert dashboard['active_model']==model
        assert dashboard['precision']==health.json()['model']['precision']


def test_model_below_quality_gate_cannot_be_activated():
    name=f'low-quality-{uuid.uuid4().hex[:8]}'
    with TestClient(app) as c:
        con=db(); con.execute("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source) VALUES(?,?,?,?,?,?,?)",(name,'ONNX','ready',50,40,now_iso(),'test')); con.commit(); con.close()
        result=c.post(f'/api/models/{name}/activate')
        assert result.status_code == 409
        assert 'ниже' in result.json()['detail']


def test_training_has_single_job_guard_and_can_be_cancelled(monkeypatch):
    release_training = asyncio.Event()

    async def blocked_training(job_id: int) -> None:
        # The cancel assertion must not race with the fixture worker completing
        # in ~0.1s on slow CI runners. Keep the fake job blocked until the
        # operator cancellation gets a chance to run.
        await release_training.wait()
        con = app_main.db()
        job = con.execute(
            "SELECT target_name,camera_id FROM training_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        con.execute(
            "UPDATE training_jobs SET status='completed',progress=100,stage='Fixture complete',updated_at=? WHERE id=?",
            (app_main.now_iso(), job_id),
        )
        con.execute(
            "INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)",
            (job[0], "ONNX FP16", "ready", 93.0, 88.0, app_main.now_iso(), f"fixture:{job[1]}", f"file:///test/{job[0]}.onnx", ""),
        )
        con.commit()
        con.close()

    monkeypatch.setattr(app_main, "run_training", blocked_training)
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
    release_training.set()
