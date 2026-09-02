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


def test_bot_api_token_reads_private_mount(tmp_path, monkeypatch):
    token_file=tmp_path/'api-token'
    token_file.write_text('service-token\n',encoding='utf-8')
    monkeypatch.setenv('ZMK_BOT_API_TOKEN_FILE',str(token_file))
    assert bot_main._bot_api_token()=='service-token'


def test_username_roles_are_case_insensitive(monkeypatch):
    previous = (set(bot_main.RUNTIME.admins), set(bot_main.RUNTIME.operators), set(bot_main.RUNTIME.viewers), set(bot_main.RUNTIME.admin_usernames), set(bot_main.RUNTIME.operator_usernames), set(bot_main.RUNTIME.viewer_usernames))
    try:
        bot_main.RUNTIME.admins = set(); bot_main.RUNTIME.operators = set(); bot_main.RUNTIME.viewers = set()
        bot_main.RUNTIME.admin_usernames = {"@chilavik"}; bot_main.RUNTIME.operator_usernames = {"@shift_operator"}; bot_main.RUNTIME.viewer_usernames = {"@safety_viewer"}
        assert role_for(999, "ChIlAvIk") == "admin"
        assert allowed(999, "admin", "@chilavik")
        assert role_for(998, "shift_operator") == "operator"
        assert not allowed(997, "operator", "safety_viewer")
        assert role_for(996) == "denied"
    finally:
        bot_main.RUNTIME.admins, bot_main.RUNTIME.operators, bot_main.RUNTIME.viewers, bot_main.RUNTIME.admin_usernames, bot_main.RUNTIME.operator_usernames, bot_main.RUNTIME.viewer_usernames = previous


def test_project_log_shipping_posts_buffered_records(monkeypatch):
    """Строки бота уходят в единый журнал проекта (вкладка «Логи»)."""
    import main as bot_main
    sent=[]
    async def fake_api(method,path,**kwargs):
        sent.append((method,path,kwargs['json'])); return {'accepted':1,'dropped':0}
    monkeypatch.setattr(bot_main,'api',fake_api)
    monkeypatch.setattr(bot_main,'LOG_SHIP_INTERVAL_SECONDS',.01)
    bot_main._log_ship_lines.clear()
    bot_main.log.warning('Telegram API недоступен, повтор через 5 с')

    async def run_once():
        task=asyncio.create_task(bot_main.log_ship_worker())
        await asyncio.sleep(.05)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run_once())

    assert len(sent)==1
    method,path,payload=sent[0]
    assert method=='POST' and path=='/api/service-logs'
    assert payload['service']=='bot-telegram'
    assert payload['entries'][0]['level']=='WARNING'
    assert 'недоступен' in payload['entries'][0]['message']
    assert payload['entries'][0]['timestamp'].endswith('+00:00')
    assert len(bot_main._log_ship_lines)==0


def test_project_log_shipping_keeps_lines_when_api_is_down(monkeypatch):
    import main as bot_main
    async def failing_api(method,path,**kwargs):
        raise RuntimeError('API недоступен')
    monkeypatch.setattr(bot_main,'api',failing_api)
    monkeypatch.setattr(bot_main,'LOG_SHIP_INTERVAL_SECONDS',.01)
    bot_main._log_ship_lines.clear()
    bot_main.log.error('Telegram polling session stopped')

    async def run_once():
        task=asyncio.create_task(bot_main.log_ship_worker())
        await asyncio.sleep(.05)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run_once())

    assert len(bot_main._log_ship_lines)==1


def test_project_log_handler_skips_service_noise_but_keeps_warnings():
    """Отправка журнала не должна порождать новые строки (поток зациклится)."""
    import logging

    import main as bot_main
    handler=bot_main._ProjectLogHandler(); handler.setFormatter(logging.Formatter('%(name)s: %(message)s'))
    noisy=logging.getLogger('httpx'); serious=logging.getLogger('zmk.telegram.noise-test')
    for logger in (noisy,serious): logger.addHandler(handler); logger.setLevel(logging.INFO)
    bot_main._log_ship_lines.clear()
    try:
        noisy.info('HTTP Request: POST http://api:8000/api/service-logs "HTTP/1.1 202 Accepted"')
        noisy.warning('HTTP Request failed: connection reset by peer')
        serious.info('Опрос Telegram запущен')
    finally:
        for logger in (noisy,serious): logger.removeHandler(handler)
    messages=[entry[2] for entry in bot_main._log_ship_lines]
    assert not any('202 Accepted' in message for message in messages)
    assert any('connection reset by peer' in message for message in messages)
    assert any('Опрос Telegram запущен' in message for message in messages)
    bot_main._log_ship_lines.clear()
