import os
from types import SimpleNamespace

os.environ.setdefault("MAX_BOT_TOKEN", "test-token")
os.environ.setdefault("MAX_ADMIN_IDS", "100")
os.environ.setdefault("MAX_OPERATOR_IDS", "200")
os.environ.setdefault("MAX_VIEWER_IDS", "300")

from main import allowed, args, dashboard_text, event_text, role_for, user_id


def test_roles():
    assert role_for(100) == "admin"
    assert role_for(200) == "operator"
    assert role_for(300) == "viewer"
    assert allowed(100, "admin")
    assert allowed(200, "operator")
    assert not allowed(300, "operator")
    assert role_for(999) == "denied"


def test_event_identity_and_command_arguments():
    event = SimpleNamespace(from_user=SimpleNamespace(user_id=100), message=SimpleNamespace(body=SimpleNamespace(text="/set_threshold helmet_conf 0.85")))
    assert user_id(event) == 100
    assert args(event) == ["helmet_conf", "0.85"]


def test_formatters():
    text = dashboard_text({"cameras": {"online": 9, "total": 10}, "events24h": 2, "critical_unacked": 1, "avg_fps": 8, "avg_latency_ms": 200, "gpu_load": 60, "active_model": "siz", "precision": 92, "recall": 87})
    assert "9/10" in text and "siz" in text
    assert "Без каски" in event_text({"type": "no_helmet", "camera_id": "cam_01", "confidence": 0.9, "severity": "high"})


def test_admin_managed_token_overrides_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "env-token")
    monkeypatch.setenv("ZMK_BOT_TOKEN_DIR", str(tmp_path))
    (tmp_path / "max.token").write_text("admin-token\n", encoding="utf-8")
    import main as bot_main
    assert bot_main.bot_token() == "admin-token"
