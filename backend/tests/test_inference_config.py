from app import main
from fastapi.testclient import TestClient

app = main.app


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


def test_bulk_acknowledgement_preserves_operator_note():
    with TestClient(app) as c:
        pending = [event for event in c.get('/api/events?limit=10').json() if not event['acknowledged']][:2]
        assert len(pending) == 2
        ids = [event['id'] for event in pending]
        result = c.post('/api/events/ack-bulk', json={'event_ids': ids, 'note': 'Проверено сменным мастером'})
        assert result.status_code == 200, result.text
        assert set(result.json()['acknowledged_ids']) == set(ids)
        events = {event['id']: event for event in c.get('/api/events?limit=100').json()}
        assert all(events[event_id]['acknowledged'] == 1 for event_id in ids)
        assert all(events[event_id]['note'] == 'Проверено сменным мастером' for event_id in ids)
        assert all(events[event_id]['review_status'] == 'accepted' for event_id in ids)
        assert c.post('/api/events/ack-bulk', json={'event_ids': [], 'note': ''}).status_code == 422


def test_event_csv_export_respects_server_side_filters():
    with TestClient(app) as c:
        con = main.db()
        con.execute(
            "INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,acknowledged,note) VALUES(?,?,?,?,?,?,?,?)",
            (main.now_iso(), 'cam_01', 'smoking', 'high', .95, 'CSV-FILTER', 0, ''),
        )
        con.commit(); con.close()
        response = c.get('/api/reports/events.csv?event_type=smoking&camera_id=cam_01&q=CSV-FILTER')
        assert response.status_code == 200
        assert 'smoking' in response.text
        assert 'no_helmet' not in response.text



def test_event_can_be_rejected_individually_or_in_bulk():
    with TestClient(app) as c:
        pending = [event for event in c.get('/api/events?limit=10').json() if event['review_status'] == 'pending']
        assert len(pending) >= 2
        first, second = pending[:2]
        single = c.post(f"/api/events/{first['id']}/reject", json={'note': 'Ложное срабатывание'})
        assert single.status_code == 200, single.text
        assert single.json()['review_status'] == 'rejected'
        bulk = c.post('/api/events/reject-bulk', json={'event_ids': [second['id']], 'note': 'Работы по обслуживанию'})
        assert bulk.status_code == 200, bulk.text
        assert bulk.json()['rejected_ids'] == [second['id']]
        events = {event['id']: event for event in c.get('/api/events?limit=100').json()}
        assert events[first['id']]['review_status'] == 'rejected'
        assert events[first['id']]['acknowledged'] == 1
        assert events[first['id']]['note'] == 'Ложное срабатывание'
        assert events[second['id']]['review_status'] == 'rejected'
        assert events[second['id']]['note'] == 'Работы по обслуживанию'
