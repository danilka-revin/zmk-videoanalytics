from datetime import datetime, timedelta, timezone

from app.main import app, csv_safe, db
from fastapi.testclient import TestClient


def test_camera_credentials_never_leave_list_endpoint():
    with TestClient(app) as c:
        cameras = c.get('/api/cameras').json()
        assert cameras
        assert all('rtsp_url' not in camera for camera in cameras)
        assert all('configured' in camera for camera in cameras)


def test_invalid_rtsp_and_detection_timestamp_are_rejected():
    with TestClient(app) as c:
        for url in ('http://example.test/video', 'rtsp://:554/stream', 'rtsp://camera:not-a-port/stream', 'rtsp://camera:70000/stream'):
            bad_camera = c.post('/api/cameras', json={'name':'Bad URL','zone':'Test','rtsp_url':url})
            assert bad_camera.status_code == 422, url
        model = next(x['name'] for x in c.get('/api/models').json() if x['active'])
        bad_detection = c.post('/api/inference/detections', json={'detections':[{
            'camera_id':'cam_01','model_name':model,'timestamp':'not-a-date',
            'event_type':'no_helmet','confidence':.99
        }]})
        assert bad_detection.status_code == 422


def test_camera_fps_limit_is_capped_at_twenty():
    with TestClient(app) as c:
        invalid = c.post('/api/cameras', json={'name':'Fast','zone':'Test','rtsp_url':'rtsp://camera/stream','fps_limit':20.1})
        assert invalid.status_code == 422
        valid = c.post('/api/cameras', json={'name':'Twenty','zone':'Test','rtsp_url':'rtsp://camera/stream','fps_limit':20})
        assert valid.status_code == 201


def test_csv_formula_injection_is_neutralized():
    assert csv_safe('=HYPERLINK("https://evil.invalid")').startswith("'=")
    assert csv_safe('+cmd') == "'+cmd"
    assert csv_safe('ordinary text') == 'ordinary text'


def test_duplicate_registered_training_target_is_rejected():
    with TestClient(app) as c:
        result = c.post('/api/training/jobs', json={
            'camera_id':'cam_01','image_count':20,'epochs':1,'target_name':'siz-guard-v2.1'
        })
        assert result.status_code == 409


def test_retention_setting_removes_expired_events_and_logs_immediately():
    with TestClient(app) as c:
        old=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
        con=db()
        con.execute("INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id) VALUES(?,?,?,?,?,?)",(old,'cam_01','no_helmet','high',.99,'OLD-EVENT'))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(old,'INFO','test','OLD-LOG'))
        con.commit(); con.close()
        changed=c.put('/api/admin/config',json={'values':{'retention_days':1}})
        assert changed.status_code==200
        con=db()
        assert con.execute("SELECT COUNT(*) FROM events WHERE person_id='OLD-EVENT'").fetchone()[0]==0
        assert con.execute("SELECT COUNT(*) FROM logs WHERE message='OLD-LOG'").fetchone()[0]==0
        con.close()
