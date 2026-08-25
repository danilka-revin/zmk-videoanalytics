import asyncio
import os

os.environ.setdefault('TELEGRAM_ADMIN_IDS','100')
os.environ.setdefault('TELEGRAM_OPERATOR_IDS','200')
os.environ.setdefault('TELEGRAM_VIEWER_IDS','300')
import main as bot_main
from main import allowed, dashboard_text, event_text, role_for


def test_roles():
    assert role_for(100)=='admin' and role_for(200)=='operator' and role_for(300)=='viewer'
    assert allowed(100,'admin') and allowed(200,'operator') and not allowed(300,'operator')
    assert role_for(999)=='denied'
def test_formatters():
    text=dashboard_text({'cameras':{'online':9,'total':10},'events24h':2,'critical_unacked':1,'avg_fps':8,'avg_latency_ms':200,'gpu_load':60,'precision':92,'recall':87})
    assert '9/10' in text and '92%' in text
    assert 'Без каски' in event_text({'type':'no_helmet','camera_id':'cam_01','confidence':.9,'severity':'high'})


def test_local_bot_menu_omits_invalid_http_mini_app_button():
    previous=bot_main.WEBAPP_URL
    try:
        bot_main.WEBAPP_URL='http://localhost:5173/telegram'
        labels=[button.text for row in bot_main.menu(100).inline_keyboard for button in row]
        assert '📊 Открыть ZMK Mini App' not in labels
        bot_main.WEBAPP_URL='https://vision.example.test/telegram'
        labels=[button.text for row in bot_main.menu(100).inline_keyboard for button in row]
        assert '📊 Открыть ZMK Mini App' in labels
    finally:
        bot_main.WEBAPP_URL=previous


def test_admin_managed_token_overrides_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','env-token')
    monkeypatch.setenv('ZMK_BOT_TOKEN_DIR',str(tmp_path))
    (tmp_path/'telegram.token').write_text('admin-token\n',encoding='utf-8')
    assert bot_main.bot_token()=='admin-token'


def test_camera_keyboard_and_snapshot_delivery(monkeypatch):
    cameras = [{"id":"cam_01","name":"Проходная","zone":"Склад","status":"online","fps":12,"snapshot_age_seconds":2}]
    keyboard = bot_main._camera_keyboard(cameras)
    assert any(button.callback_data == "camera:cam_01" for row in keyboard.inline_keyboard for button in row)

    class Message:
        def __init__(self): self.photos=[]; self.answers=[]
        async def answer(self, *args, **kwargs): self.answers.append((args,kwargs))
        async def answer_photo(self, *args, **kwargs): self.photos.append((args,kwargs))

    async def fake_api(method, path, **_kwargs):
        if path == "/api/cameras": return cameras
        if path == "/api/cameras/cam_01/snapshot": return b"\xff\xd8frame\xff\xd9"
        raise AssertionError(path)

    monkeypatch.setattr(bot_main, "api", fake_api)
    message = Message()
    asyncio.run(bot_main._send_camera_snapshot(message, "cam_01"))
    assert message.photos
    assert "Проходная" in message.photos[0][1]["caption"]
