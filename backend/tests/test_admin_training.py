import time
import uuid

from app.main import app, db, now_iso
from fastapi.testclient import TestClient


def test_error_report_and_csv():
    with TestClient(app) as c:
        con=db(); con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(now_iso(),'ERROR','test','Fixture error','cam_01')); con.commit(); con.close()
        report = c.get('/api/reports/errors?hours=24')
        assert report.status_code == 200
        assert set(report.json()['summary']) == {'WARNING', 'ERROR', 'CRITICAL'}
        csv = c.get('/api/reports/errors.csv?hours=24')
        assert csv.status_code == 200
        assert 'text/csv' in csv.headers['content-type']


def test_hot_swap_is_atomic_and_audited():
    with TestClient(app) as c:
        models = c.get('/api/models').json()
        target = next(x['name'] for x in models if not x['active'] and x['status'] == 'ready')
        result = c.post(f'/api/models/{target}/activate')
        assert result.status_code == 200
        assert result.json()['hot_swap'] is True
        assert result.json()['downtime_ms'] == 0
        models = c.get('/api/models').json()
        assert next(x for x in models if x['name'] == target)['active'] is True
        assert c.post('/api/models/missing/activate').status_code == 404


def test_training_rejects_offline_camera_and_completes_online_job():
    with TestClient(app) as c:
        assert c.post('/api/training/jobs', json={'camera_id':'cam_07'}).status_code == 409
        target_name = f"test-camera-{uuid.uuid4().hex[:10]}"
        started = c.post('/api/training/jobs', json={'camera_id':'cam_01','image_count':20,'epochs':1,'target_name':target_name,'batch':4,'imgsz':512,'patience':5,'confidence':.4,'val_split':.25,'capture_fps':1})
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
        assert (job['batch'],job['imgsz'],job['patience'],job['confidence'],job['val_split'],job['capture_fps']) == (4,512,5,.4,.25,1)
        models = c.get('/api/models').json()
        assert any(x['name'] == target_name and x['status'] == 'ready' for x in models)


def test_admin_bot_workspace_controls_runtime_roles_alerts_and_test_queue(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'telegram-test-token')
    monkeypatch.delenv('MAX_BOT_TOKEN', raising=False)
    with TestClient(app) as c:
        initial = c.get('/api/admin/bots')
        assert initial.status_code == 200
        telegram = next(item for item in initial.json()['providers'] if item['provider'] == 'telegram')
        assert telegram['token_configured'] is True

        saved = c.put('/api/admin/bots/telegram', json={
            'enabled': True,
            'alerts_enabled': True,
            'alert_min_severity': 'high',
            'admin_ids': '100, 200',
            'operator_ids': '300',
            'viewer_ids': '400',
            'alert_recipients': '-100555, 100',
            'webapp_url': 'https://vision.example.test/telegram',
        })
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body['enabled'] is True
        assert body['admin_ids'] == '100,200'
        assert body['alert_recipients'] == '-100555,100'
        assert body['webapp_url'] == 'https://vision.example.test/telegram'

        runtime = c.get('/api/bots/telegram/runtime').json()
        assert runtime['enabled'] is True
        assert runtime['admin_ids'] == [100, 200]
        assert runtime['alert_recipients'] == [-100555, 100]

        heartbeat = c.post('/api/bots/telegram/heartbeat', json={'status': 'active', 'detail': 'polling', 'enabled': True})
        assert heartbeat.status_code == 200
        queued = c.post('/api/admin/bots/telegram/test-alert')
        assert queued.status_code == 202
        command_id = queued.json()['id']
        commands = c.get('/api/bots/telegram/commands').json()['commands']
        assert any(item['id'] == command_id and item['action'] == 'test_alert' for item in commands)
        assert c.post(f'/api/bots/telegram/commands/{command_id}/complete', json={'status': 'completed'}).status_code == 200
        listed = c.get('/api/admin/bots').json()
        telegram = next(item for item in listed['providers'] if item['provider'] == 'telegram')
        assert telegram['runtime']['status'] == 'active'
        assert telegram['last_test']['status'] == 'completed'

        bad_ids = c.put('/api/admin/bots/telegram', json={
            'enabled': True, 'alerts_enabled': False, 'alert_min_severity': 'high',
            'admin_ids': 'not-an-id', 'operator_ids': '', 'viewer_ids': '', 'alert_recipients': '',
        })
        assert bad_ids.status_code == 422
        missing_token = c.put('/api/admin/bots/max', json={
            'enabled': True, 'alerts_enabled': False, 'alert_min_severity': 'high',
            'admin_ids': '', 'operator_ids': '', 'viewer_ids': '', 'alert_recipients': '',
        })
        assert missing_token.status_code == 422
