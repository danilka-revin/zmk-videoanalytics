from fastapi.testclient import TestClient
from app.main import app, csv_safe


def test_camera_credentials_never_leave_list_endpoint():
    with TestClient(app) as c:
        cameras = c.get('/api/cameras').json()
        assert cameras
        assert all('rtsp_url' not in camera for camera in cameras)
        assert all('configured' in camera for camera in cameras)


def test_invalid_rtsp_and_detection_timestamp_are_rejected():
    with TestClient(app) as c:
        bad_camera = c.post('/api/cameras', json={'name':'Bad URL','zone':'Test','rtsp_url':'http://example.test/video'})
        assert bad_camera.status_code == 422
        model = next(x['name'] for x in c.get('/api/models').json() if x['active'])
        bad_detection = c.post('/api/inference/detections', json={'detections':[{
            'camera_id':'cam_01','model_name':model,'timestamp':'not-a-date',
            'event_type':'no_helmet','confidence':.99
        }]})
        assert bad_detection.status_code == 422


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
