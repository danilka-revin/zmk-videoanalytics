import os
os.environ.setdefault('TELEGRAM_ADMIN_IDS','100')
os.environ.setdefault('TELEGRAM_OPERATOR_IDS','200')
os.environ.setdefault('TELEGRAM_VIEWER_IDS','300')
from main import role_for, allowed, dashboard_text, event_text

def test_roles():
    assert role_for(100)=='admin' and role_for(200)=='operator' and role_for(300)=='viewer'
    assert allowed(100,'admin') and allowed(200,'operator') and not allowed(300,'operator')
    assert role_for(999)=='denied'
def test_formatters():
    text=dashboard_text({'cameras':{'online':9,'total':10},'events24h':2,'critical_unacked':1,'avg_fps':8,'avg_latency_ms':200,'gpu_load':60,'precision':92,'recall':87})
    assert '9/10' in text and '92%' in text
    assert 'Без каски' in event_text({'type':'no_helmet','camera_id':'cam_01','confidence':.9,'severity':'high'})
