from app.main import app
from fastapi.testclient import TestClient


def test_health_and_dashboard():
    with TestClient(app) as c:
        assert c.get('/api/health').status_code == 200
        d=c.get('/api/dashboard').json()
        assert d['cameras']['total'] == 10
        assert d['messenger_provider'] in {'none','telegram','max'}

def test_ack_existing_event():
    with TestClient(app) as c:
        event=c.get('/api/events?limit=1').json()[0]
        assert c.post(f"/api/events/{event['id']}/ack",json={'note':'Проверено'}).status_code == 200


def test_capabilities_expose_inference_worker_flag():
    with TestClient(app) as c:
        cap=c.get('/api/capabilities').json()
        assert 'inference_worker' in cap
        assert isinstance(cap['inference_worker'], bool)
        assert 'fresh_snapshots' in cap


def test_camera_edit_preserves_rtsp_url_and_never_leaks_it():
    with TestClient(app) as c:
        created=c.post('/api/cameras',json={'name':'Sec Cam','zone':'Z','rtsp_url':'rtsp://u:p@cam/stream','fps_limit':8}).json()
        cid=created['id']
        # The camera list must NOT expose the raw URL.
        item=next(x for x in c.get('/api/cameras').json() if x['id']==cid)
        assert 'rtsp_url' not in item or item.get('rtsp_url') in (None,'')
        assert bool(item.get('configured')) is True
        # Edit WITHOUT a new URL: null must keep the existing one.
        r=c.put(f'/api/cameras/{cid}',json={'name':'Sec Cam 2','zone':'Z','rtsp_url':None,'fps_limit':8,'enabled':True})
        assert r.status_code==200 and bool(r.json()['configured']) is True
        # Also, sending an empty string must NOT wipe it either.
        c.put(f'/api/cameras/{cid}',json={'name':'Sec Cam 3','zone':'Z','rtsp_url':'','fps_limit':8,'enabled':True})
        # Raw URL is still stored and not leaked.
        item2=next(x for x in c.get('/api/cameras').json() if x['id']==cid)
        assert bool(item2.get('configured')) is True
        assert 'rtsp_url' not in item2 or item2.get('rtsp_url') in (None,'')


def test_telemetry_accepts_recovering_status():
    # The worker now reports 'recovering' (after transient read failures);
    # previously this was rejected with 422 so the camera status never updated.
    with TestClient(app) as c:
        cid=c.post('/api/cameras',json={'name':'Rec','zone':'Z','rtsp_url':'rtsp://u:p@h/s','fps_limit':8}).json()['id']
        r=c.post(f'/api/cameras/{cid}/telemetry',json={'status':'recovering','fps':2,'latency_ms':40})
        assert r.status_code==200, r.text
        item=next(x for x in c.get('/api/cameras').json() if x['id']==cid)
        assert item['status']=='recovering'
