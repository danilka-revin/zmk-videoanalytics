"""Admin-entered messenger tokens remain write-only and private to workers."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import time
from urllib.parse import urlencode

from app import main
from fastapi.testclient import TestClient


def bot_payload(*, enabled: bool, token: object | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "enabled": enabled,
        "alerts_enabled": True,
        "alert_min_severity": "high",
        "admin_ids": "100",
        "operator_ids": "200",
        "viewer_ids": "300",
        "alert_recipients": "-100555,100",
        "webapp_url": "https://vision.example.test/telegram",
    }
    if token is not None:
        payload["token"] = token
    return payload


def telegram_init_data(token: str, user_id: int, username: str = "") -> str:
    user = {"id": user_id}
    if username:
        user["username"] = username
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "admin-token-test",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_admin_can_save_and_enable_write_only_bot_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    secret = "123456789:AA_write_only_admin_token_123456"

    with TestClient(main.app) as client:
        saved = client.put("/api/admin/bots/telegram", json=bot_payload(enabled=True, token=secret))
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["enabled"] is True
        assert body["token_configured"] is True
        assert body["token_source"] == "admin"
        assert secret not in saved.text
        assert "token" not in body

        # The dashboard, runtime API and subsequent Admin reads only reveal
        # status/source metadata, never the credential itself.
        listed = client.get("/api/admin/bots")
        runtime = client.get("/api/bots/telegram/runtime")
        assert listed.status_code == runtime.status_code == 200
        assert secret not in listed.text
        assert secret not in runtime.text
        telegram = next(item for item in listed.json()["providers"] if item["provider"] == "telegram")
        assert telegram["token_configured"] is True
        assert telegram["token_source"] == "admin"

        # The same write-only value becomes the Telegram Mini App HMAC secret;
        # production API-key protection therefore works without an .env token.
        monkeypatch.setattr(main, "API_KEY", "protected-api")
        mini_app = client.get("/api/dashboard", headers={"X-Telegram-Init-Data": telegram_init_data(secret, 100)})
        assert mini_app.status_code == 200

    path = main._managed_bot_token_path("telegram")
    assert path.read_text(encoding="utf-8").strip() == secret
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_admin_token_is_not_reflected_in_validation_error(monkeypatch):
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    secret = "must not appear in response"

    with TestClient(main.app) as client:
        result = client.put("/api/admin/bots/max", json=bot_payload(enabled=False, token=secret))

    assert result.status_code == 422
    assert secret not in result.text
    assert "управляющие символы" in result.text


def test_admin_token_takes_precedence_over_legacy_environment(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-env-token")
    admin_token = "admin-max-token-1234567890"

    with TestClient(main.app) as client:
        saved = client.put("/api/admin/bots/max", json=bot_payload(enabled=True, token=admin_token))
        assert saved.status_code == 200, saved.text
        assert saved.json()["token_source"] == "admin"

    assert main._effective_bot_token("max") == admin_token


def test_telegram_username_role_allows_bot_and_mini_app(monkeypatch):
    secret = "123456789:AA_username_role_test_token_123456"
    with TestClient(main.app) as client:
        payload = bot_payload(enabled=True, token=secret)
        payload.update({"admin_ids": "@ChIlAvIk", "operator_ids": "@shift_operator", "viewer_ids": "@safety_viewer"})
        saved = client.put("/api/admin/bots/telegram", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["admin_ids"] == "@chilavik"

        runtime = client.get("/api/bots/telegram/runtime")
        assert runtime.status_code == 200, runtime.text
        assert runtime.json()["admin_ids"] == []
        assert runtime.json()["admin_usernames"] == ["@chilavik"]
        assert runtime.json()["operator_usernames"] == ["@shift_operator"]

        monkeypatch.setattr(main, "API_KEY", "protected-api")
        headers = {"X-Telegram-Init-Data": telegram_init_data(secret, 501, "ChIlAvIk")}
        assert client.get("/api/dashboard", headers=headers).status_code == 200
        session = client.get("/api/session", headers=headers)
        assert session.status_code == 200 and session.json()["role"] == "admin"
        assert client.get("/api/dashboard", headers={"X-Telegram-Init-Data": telegram_init_data(secret, 502, "other_user")}).status_code == 401
