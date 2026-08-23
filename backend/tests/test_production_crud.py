import base64

from app import main
from fastapi.testclient import TestClient


def test_production_starts_without_fake_entities(monkeypatch):
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    with TestClient(main.app) as c:
        assert c.get('/api/cameras').json() == []
        assert c.get('/api/events').json() == []
        assert c.get('/api/models').json() == []
        assert c.get('/api/admin/users').json() == []
        dashboard=c.get('/api/dashboard').json()
        assert dashboard['cameras']=={'total':0,'online':0}
        assert dashboard['active_model'] is None
        assert dashboard['precision'] is None
        assert c.post('/api/events/simulate').status_code==404
        assert c.post('/api/admin/logs/simulate-error').status_code==404
        assert c.post('/api/training/jobs',json={'camera_id':'missing'}).status_code==503


def test_bootstraps_first_camera_from_rtsp_environment(monkeypatch):
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    monkeypatch.setenv("RTSP_CAM_01", "rtsp://user:password@camera.internal:554/stream")
    with TestClient(main.app) as c:
        cameras = c.get('/api/cameras').json()
        assert len(cameras) == 1
        camera = cameras[0]
        assert camera['id'] == 'cam_env_01'
        assert camera['configured'] == 1 and camera['enabled'] == 1
        assert 'rtsp_url' not in camera
        assert c.get('/api/dashboard').json()['cameras'] == {'total': 1, 'online': 0}

    # Re-entering lifespan must not duplicate or overwrite the configured URL.
    with TestClient(main.app) as c:
        assert len(c.get('/api/cameras').json()) == 1


def test_camera_full_crud_search_telemetry_and_diagnostics(monkeypatch):
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    with TestClient(main.app) as c:
        created=c.post('/api/cameras',json={'name':'Склад Север','zone':'Склад','description':'Ворота №2','rtsp_url':'rtsp://127.0.0.1:9/stream','fps_limit':6,'enabled':True})
        assert created.status_code==201
        camera_id=created.json()['id']
        camera=c.get(f'/api/cameras/{camera_id}').json()
        assert camera['description']=='Ворота №2' and camera['fps_limit']==6
        assert 'rtsp_url' not in camera
        updated=c.put(f'/api/cameras/{camera_id}',json={'name':'Склад Южный','zone':'Склад','description':'После переноса','rtsp_url':None,'fps_limit':4.5,'enabled':True})
        assert updated.status_code==200
        telemetry=c.post(f'/api/cameras/{camera_id}/telemetry',json={'status':'online','fps':4.2,'latency_ms':180})
        assert telemetry.status_code==200
        results=c.get('/api/search',params={'q':'Южный'}).json()['results']
        assert results and results[0]['kind']=='camera'
        diag=c.post(f'/api/cameras/{camera_id}/diagnostics').json()
        assert diag['reachable'] is False and diag['status']=='unreachable'
        jpeg=b'\xff\xd8'+b'camera-frame'+b'\xff\xd9'
        assert c.post(f'/api/cameras/{camera_id}/snapshot',json={'jpeg_base64':base64.b64encode(jpeg).decode()}).status_code==204
        assert c.get(f'/api/cameras/{camera_id}/snapshot').status_code==200
        assert c.delete(f'/api/cameras/{camera_id}').status_code==200
        assert c.get(f'/api/cameras/{camera_id}/snapshot').status_code==404
        assert c.get('/api/cameras').json()==[]


def test_stale_camera_telemetry_is_not_presented_as_online(monkeypatch):
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    with TestClient(main.app) as c:
        camera_id = c.post('/api/cameras', json={'name':'Старая телеметрия','zone':'Цех','rtsp_url':'rtsp://camera/stream'}).json()['id']
        assert c.post(f'/api/cameras/{camera_id}/telemetry', json={'status':'online','fps':8,'latency_ms':20}).status_code == 200
        con = main.db()
        con.execute("UPDATE cameras SET telemetry_at=? WHERE id=?", ('2000-01-01T00:00:00+07:00', camera_id))
        con.commit(); con.close()

        camera = c.get(f'/api/cameras/{camera_id}').json()
        assert camera['status'] == 'offline'
        assert camera['telemetry_stale'] is True
        assert c.get('/api/dashboard').json()['cameras']['online'] == 0


def test_camera_stream_state_resets_on_reconfigure_or_disable(monkeypatch):
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    with TestClient(main.app) as c:
        created = c.post('/api/cameras', json={'name':'Линия 1','zone':'Цех','rtsp_url':'rtsp://camera/one'}).json()
        camera_id = created['id']
        assert c.post(f'/api/cameras/{camera_id}/telemetry', json={'status':'online','fps':5,'latency_ms':40}).status_code == 200
        changed = c.put(f'/api/cameras/{camera_id}', json={'name':'Линия 1','zone':'Цех','description':'','rtsp_url':'rtsp://camera/two','fps_limit':8,'enabled':True})
        assert changed.status_code == 200 and changed.json()['stream_reset'] is True
        camera = c.get(f'/api/cameras/{camera_id}').json()
        assert camera['status'] == 'connecting' and camera['fps'] == 0

        toggled = c.patch(f'/api/cameras/{camera_id}/toggle')
        assert toggled.status_code == 200 and toggled.json()['enabled'] is False
        # A frame already in flight from the worker must not switch the disabled
        # camera back to online.
        ignored = c.post(f'/api/cameras/{camera_id}/telemetry', json={'status':'online','fps':5,'latency_ms':40})
        assert ignored.status_code == 200 and ignored.json()['ignored'] is True
        camera = c.get(f'/api/cameras/{camera_id}').json()
        assert camera['status'] == 'unknown'


def test_camera_delete_requires_explicit_event_confirmation():
    with TestClient(main.app) as c:
        assert c.delete('/api/cameras/cam_01').status_code==409
        deleted=c.delete('/api/cameras/cam_01',params={'delete_events':True})
        assert deleted.status_code==200 and deleted.json()['deleted_events']>0


def test_external_model_registration_without_seed_data(monkeypatch):
    monkeypatch.setattr(main, "SEED_TEST_DATA", False)
    with TestClient(main.app) as c:
        model={'name':'plant-safety-v1','format':'ONNX','precision':93.2,'recall':88.1,'source':'mlops','artifact_uri':'file:///models/plant-safety-v1.onnx','checksum':'a'*64}
        registered=c.post('/api/models',json=model)
        assert registered.status_code==201
        assert c.post('/api/models/plant-safety-v1/activate').status_code==200
        health=c.get('/api/models/active/health').json()
        assert health['healthy'] is True and health['model']['name']=='plant-safety-v1'


def test_system_health_contains_measured_values():
    with TestClient(main.app) as c:
        health=c.get('/api/system-health').json()
        assert 0<=health['cpu']<=100
        assert 0<=health['ram']<=100
        assert 0<=health['disk']<=100
        assert health['gpu'] is None or 0<=health['gpu']<=100
