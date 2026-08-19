from fastapi.testclient import TestClient
from app.main import app


def active_model(client):
    return next(x['name'] for x in client.get('/api/models').json() if x['active'])


def test_model_to_event_data_flow_accepts_and_filters_batch():
    with TestClient(app) as c:
        model = active_model(c)
        payload = {'detections': [
            {'camera_id':'cam_01','model_name':model,'event_type':'no_helmet','confidence':.96,'person_id':'FLOW-1','bbox':[10,20,100,200]},
            {'camera_id':'cam_01','model_name':model,'event_type':'no_helmet','confidence':.11,'person_id':'FLOW-2'},
            {'camera_id':'cam_01','model_name':'stale-model','event_type':'smoking','confidence':.99}
        ]}
        result = c.post('/api/inference/detections', json=payload)
        assert result.status_code == 200
        body = result.json()
        assert body['received'] == 3
        assert len(body['accepted']) == 1
        assert len(body['rejected']) == 2
        assert any('below_threshold' in x['reason'] for x in body['rejected'])
        assert any('stale_model' in x['reason'] for x in body['rejected'])
        events = c.get('/api/events?limit=100').json()
        assert any(x['person_id'] == 'FLOW-1' for x in events)
        assert not any(x['person_id'] == 'FLOW-2' for x in events)


def test_admin_config_roundtrip_and_validation():
    with TestClient(app) as c:
        before = c.get('/api/admin/config')
        assert before.status_code == 200
        assert 'inference' in before.json() and 'integration' in before.json()
        changed = c.put('/api/admin/config', json={'values': {'inference_fps':'9','nms_iou':'0.5','webhook_enabled':True,'webhook_url':'https://skud.internal/events'}})
        assert changed.status_code == 200
        after = c.get('/api/admin/config').json()
        assert after['inference']['inference_fps'] == '9'
        assert after['integration']['webhook_enabled'] == 'true'
        assert c.put('/api/admin/config', json={'values': {'inference_fps':100}}).status_code == 422
        assert c.put('/api/admin/config', json={'values': {'unknown_key':'x'}}).status_code == 422


def test_user_rbac_safety():
    with TestClient(app) as c:
        users = c.get('/api/admin/users')
        assert users.status_code == 200
        assert {x['role'] for x in users.json()} >= {'admin','operator','viewer'}
        admin = next(x for x in users.json() if x['role'] == 'admin')
        assert c.patch(f"/api/admin/users/{admin['id']}/toggle").status_code == 409
