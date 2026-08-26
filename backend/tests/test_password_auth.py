"""Password gate, change flow and email-code recovery contracts."""
from __future__ import annotations

from app import main
from fastapi.testclient import TestClient


def enable_password_auth(monkeypatch):
    monkeypatch.setattr(main, "PASSWORD_AUTH_ENABLED", True)
    monkeypatch.setattr(main, "INITIAL_APP_PASSWORD", "1234")
    main._auth_attempts.clear()


def test_password_gate_login_change_and_logout(monkeypatch):
    enable_password_auth(monkeypatch)
    with TestClient(main.app) as client:
        assert client.get("/api/dashboard").status_code == 401
        status = client.get("/api/auth/status").json()
        assert status["enabled"] is True and status["authenticated"] is False
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        login = client.post("/api/auth/login", json={"password": "1234"})
        assert login.status_code == 200, login.text
        assert login.json()["must_change"] is True
        assert client.get("/api/dashboard").status_code == 403

        changed = client.put("/api/auth/password", json={"current_password": "1234", "new_password": "new-password-42"})
        assert changed.status_code == 200, changed.text
        assert changed.json()["changed"] is True
        assert client.get("/api/dashboard").status_code == 200
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/dashboard").status_code == 401
        assert client.post("/api/auth/login", json={"password": "1234"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": "new-password-42"}).status_code == 200


def test_legacy_first_password_is_corrected_without_touching_changed_password(monkeypatch):
    enable_password_auth(monkeypatch)
    assert main._resolve_initial_app_password("1243") == "1234"
    assert main._resolve_initial_app_password("operator-secret") == "operator-secret"

    with TestClient(main.app) as client:
        con = main.db()
        con.execute("UPDATE settings SET value=? WHERE key='auth_password_hash'", (main._hash_password("1243"),))
        con.execute("UPDATE settings SET value='true' WHERE key='auth_password_must_change'")
        con.execute("DELETE FROM settings WHERE key='auth_initial_password_version'")
        main._initialize_or_upgrade_auth_password(con)
        con.commit()
        con.close()

        assert client.post("/api/auth/login", json={"password": "1243"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": "1234"}).status_code == 200

        # A password that was already changed by its owner is never rewritten
        # by the compatibility migration, even if an old version marker is absent.
        con = main.db()
        con.execute("UPDATE settings SET value=? WHERE key='auth_password_hash'", (main._hash_password("owner-selected-password"),))
        con.execute("UPDATE settings SET value='false' WHERE key='auth_password_must_change'")
        con.execute("DELETE FROM settings WHERE key='auth_initial_password_version'")
        main._initialize_or_upgrade_auth_password(con)
        persisted = con.execute("SELECT value FROM settings WHERE key='auth_password_hash'").fetchone()[0]
        con.commit()
        con.close()
        assert main._password_matches("owner-selected-password", persisted)


def test_bot_service_token_keeps_messenger_control_plane_working(monkeypatch):
    enable_password_auth(monkeypatch)
    monkeypatch.setattr(main, "BOT_API_TOKEN", "bot-service-secret")
    with TestClient(main.app) as client:
        assert client.get("/api/dashboard").status_code == 401
        assert client.get("/api/dashboard", headers={"X-Bot-Service-Token": "bot-service-secret"}).status_code == 200


def test_email_binding_and_recovery_code_resets_password(monkeypatch):
    enable_password_auth(monkeypatch)
    sent: dict[str, str] = {}

    def fake_send(address: str, code: str):
        sent["address"] = address
        sent["code"] = code

    monkeypatch.setattr(main, "_smtp_ready", lambda: True)
    monkeypatch.setattr(main, "_send_recovery_email", fake_send)
    with TestClient(main.app) as client:
        assert client.post("/api/auth/login", json={"password": "1234"}).status_code == 200
        assert client.put("/api/auth/password", json={"current_password": "1234", "new_password": "recovery-start-password"}).status_code == 200
        bound = client.put("/api/auth/email", json={"email": "owner@example.test", "password": "recovery-start-password"})
        assert bound.status_code == 200, bound.text
        assert bound.json()["email"] == "o***@example.test"
        request = client.post("/api/auth/recovery/request", json={"email": "owner@example.test"})
        assert request.status_code == 200, request.text
        assert sent["address"] == "owner@example.test" and len(sent["code"]) == 6
        reset = client.post("/api/auth/recovery/verify", json={"email": "owner@example.test", "code": sent["code"], "new_password": "recovered-password"})
        assert reset.status_code == 200, reset.text
        assert client.post("/api/auth/logout").status_code == 200
        assert client.post("/api/auth/login", json={"password": "recovered-password"}).status_code == 200


def test_password_session_list_and_remote_revocation(monkeypatch):
    enable_password_auth(monkeypatch)
    with TestClient(main.app) as client:
        assert client.post("/api/auth/login", json={"password": "1234"}).status_code == 200
        assert client.put("/api/auth/password", json={"current_password": "1234", "new_password": "session-control-password"}).status_code == 200
        # A second sign-in produces another valid browser session while keeping
        # this client's latest cookie as the current one.
        assert client.post("/api/auth/login", json={"password": "session-control-password"}).status_code == 200
        listed = client.get("/api/auth/sessions")
        assert listed.status_code == 200, listed.text
        sessions = listed.json()["sessions"]
        assert len(sessions) == 2
        assert sum(bool(session["current"]) for session in sessions) == 1
        other = next(session for session in sessions if not session["current"])
        revoked = client.delete(f"/api/auth/sessions/{other['id']}")
        assert revoked.status_code == 200 and revoked.json()["current"] is False
        assert len(client.get("/api/auth/sessions").json()["sessions"]) == 1


def test_smtp_implicit_ssl_skips_starttls(monkeypatch):
    calls: list[str] = []

    class FakeClient:
        def __enter__(self):
            calls.append("enter")
            return self

        def __exit__(self, *_args):
            calls.append("exit")

        def starttls(self, **_kwargs):
            calls.append("starttls")

        def login(self, *_args):
            calls.append("login")

        def send_message(self, _message):
            calls.append("send")

    monkeypatch.setattr(main, "_smtp_ready", lambda: True)
    monkeypatch.setattr(main, "SMTP_USE_SSL", True)
    monkeypatch.setattr(main, "SMTP_USE_TLS", True)
    monkeypatch.setattr(main.smtplib, "SMTP_SSL", lambda *_args, **_kwargs: FakeClient())
    main._send_recovery_email("owner@example.test", "123456")
    assert "send" in calls and "starttls" not in calls
