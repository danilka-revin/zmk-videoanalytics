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
        # Omitting `login` keeps the historical behaviour: the admin signs in.
        login = client.post("/api/auth/login", json={"password": "1234"})
        assert login.status_code == 200, login.text
        assert login.json()["login"] == "admin" and login.json()["role"] == "admin"
        # The panel ships with ready-to-use accounts, so the first login is not
        # blocked by a forced password change any more.
        assert login.json()["must_change"] is False
        assert client.get("/api/dashboard").status_code == 200

        changed = client.put("/api/auth/password", json={"current_password": "1234", "new_password": "new-password-42"})
        assert changed.status_code == 200, changed.text
        assert changed.json()["changed"] is True
        assert client.get("/api/dashboard").status_code == 200
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/dashboard").status_code == 401
        assert client.post("/api/auth/login", json={"password": "1234"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": "new-password-42"}).status_code == 200


def test_forced_change_gate_still_blocks_a_flagged_admin_session(monkeypatch):
    """Databases that still require the first change keep enforcing it."""
    enable_password_auth(monkeypatch)
    with TestClient(main.app) as client:
        con = main.db()
        con.execute("UPDATE settings SET value='true' WHERE key='auth_password_must_change'")
        con.commit(); con.close()
        assert client.get("/api/auth/status").json()["must_change"] is True
        assert client.post("/api/auth/login", json={"password": "1234"}).json()["must_change"] is True
        assert client.get("/api/dashboard").status_code == 403
        assert client.put("/api/auth/password", json={"current_password": "1234", "new_password": "chosen-password"}).status_code == 200
        assert client.get("/api/dashboard").status_code == 200


def test_two_accounts_admin_and_read_only_director(monkeypatch):
    """admin / директор are separate password accounts with separate rights."""
    enable_password_auth(monkeypatch)
    # This contract covers the shipped defaults, not the fixture override.
    monkeypatch.setattr(main, "INITIAL_APP_PASSWORD", main.DEFAULT_INITIAL_APP_PASSWORD)
    assert main.DEFAULT_INITIAL_APP_PASSWORD == "admin"
    with TestClient(main.app) as client:
        status = client.get("/api/auth/status").json()
        assert [account["login"] for account in status["accounts"]] == ["admin", "director"]
        assert status["role"] == ""  # nobody is signed in yet

        # Wrong password for the chosen account is rejected, and the director
        # password does not open the administrator account.
        assert client.post("/api/auth/login", json={"login": "admin", "password": "director"}).status_code == 401
        # An unknown login must be indistinguishable from a wrong password.
        assert client.post("/api/auth/login", json={"login": "unknown", "password": "admin"}).status_code == 401
        assert client.post("/api/auth/login", json={"login": "BAD LOGIN", "password": "admin"}).status_code == 422

        admin = TestClient(main.app)
        signed_in = admin.post("/api/auth/login", json={"login": "admin", "password": "admin"})
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json()["role"] == "admin"
        assert admin.get("/api/auth/status").json()["label"] == "Администратор"
        assert admin.get("/api/models").status_code == 200
        assert admin.post("/api/cameras", json={"name": "Новая камера", "zone": "Цех", "description": "", "rtsp_url": "rtsp://x/y", "fps_limit": 30, "enabled": True}).status_code == 201

        director = TestClient(main.app)
        signed_in = director.post("/api/auth/login", json={"login": "director", "password": "director"})
        assert signed_in.status_code == 200, signed_in.text
        # «Директор» is the built-in read-only seat: role `viewer`.
        assert signed_in.json()["role"] == "viewer"
        assert director.get("/api/auth/status").json()["label"] == "Директор"
        # Infographics, cameras and events stay readable…
        assert director.get("/api/dashboard").status_code == 200
        assert director.get("/api/cameras").status_code == 200
        assert director.get("/api/events?limit=5").status_code == 200
        assert director.get("/api/analytics/overview?hours=24&bucket=auto").status_code == 200
        # …everything that configures the platform is refused by the API itself.
        assert director.get("/api/models").status_code == 403
        assert director.get("/api/admin/summary").status_code == 403
        assert director.get("/api/logs/project").status_code == 403
        assert director.put("/api/settings/helmet_conf", json={"value": 0.9}).status_code == 403
        assert director.post("/api/events/1/ack", json={"note": "x"}).status_code == 403
        # The director may change only its own password, and only with it.
        assert director.put("/api/auth/password", json={"current_password": "admin", "new_password": "stolen-admin", "login": "admin"}).status_code == 403
        assert director.put("/api/auth/password", json={"current_password": "director", "new_password": "director-pass-2"}).status_code == 200
        assert director.post("/api/auth/logout").status_code == 200
        assert director.post("/api/auth/login", json={"login": "director", "password": "director-pass-2"}).status_code == 200


def test_admin_resets_director_password(monkeypatch):
    enable_password_auth(monkeypatch)
    monkeypatch.setattr(main, "INITIAL_APP_PASSWORD", main.DEFAULT_INITIAL_APP_PASSWORD)
    with TestClient(main.app) as client:
        assert client.post("/api/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        reset = client.put("/api/auth/password", json={"current_password": "admin", "new_password": "board-2026", "login": "director"})
        assert reset.status_code == 200, reset.text
        assert reset.json()["login"] == "director"
        # The administrator keeps its own session while the director must use
        # the new password.
        assert client.get("/api/dashboard").status_code == 200
        assert client.post("/api/auth/logout").status_code == 200
        assert client.post("/api/auth/login", json={"login": "director", "password": "director"}).status_code == 401
        assert client.post("/api/auth/login", json={"login": "director", "password": "board-2026"}).status_code == 200


def test_legacy_first_password_is_corrected_without_touching_changed_password(monkeypatch):
    enable_password_auth(monkeypatch)
    assert main._resolve_initial_app_password("1243") == main.DEFAULT_INITIAL_APP_PASSWORD
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


def test_admin_creates_analyst_and_guest_accounts(monkeypatch):
    """The administrator builds the team: аналитик reviews, гость only looks."""
    enable_password_auth(monkeypatch)
    monkeypatch.setattr(main, "INITIAL_APP_PASSWORD", main.DEFAULT_INITIAL_APP_PASSWORD)
    with TestClient(main.app) as admin:
        assert admin.post("/api/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200

        created = admin.post("/api/auth/accounts", json={"login": "ivanov", "label": "Иванов · аналитик", "role": "analyst", "password": "shift-2026"})
        assert created.status_code == 200, created.text
        assert created.json()["account"]["role"] == "analyst"
        assert admin.post("/api/auth/accounts", json={"login": "guest", "label": "Гость площадки", "role": "viewer", "password": "guest-2026"}).status_code == 200
        # Duplicates and malformed logins are refused.
        assert admin.post("/api/auth/accounts", json={"login": "ivanov", "label": "Дубль", "role": "viewer", "password": "whatever"}).status_code == 409
        assert admin.post("/api/auth/accounts", json={"login": "не логин", "label": "Плохой", "role": "viewer", "password": "whatever"}).status_code == 422
        listed = admin.get("/api/auth/accounts").json()
        assert {account["login"] for account in listed["accounts"]} == {"admin", "director", "ivanov", "guest"}

    # The new accounts appear on the login screen for everybody. A bare client
    # (no startup) reuses the already seeded database.
    anonymous = TestClient(main.app)
    pickable = [account["login"] for account in anonymous.get("/api/auth/status").json()["accounts"]]
    assert pickable == ["admin", "director", "guest", "ivanov"]

    analyst = TestClient(main.app)
    assert analyst.post("/api/auth/login", json={"login": "ivanov", "password": "shift-2026"}).status_code == 200
    assert analyst.get("/api/auth/status").json()["label"] == "Иванов · аналитик"
    # Аналитик reads everything the dashboard needs…
    assert analyst.get("/api/dashboard").status_code == 200
    assert analyst.get("/api/analytics/overview?hours=24&bucket=auto").status_code == 200
    # …works the review queue…
    event_id = analyst.get("/api/events?limit=1").json()[0]["id"]
    assert analyst.post(f"/api/events/{event_id}/ack", json={"note": "Проверено аналитиком"}).status_code == 200
    assert analyst.post(f"/api/events/{event_id}/reject", json={"note": "Ложное срабатывание"}).status_code == 200
    assert analyst.post("/api/events/ack-bulk", json={"event_ids": [event_id], "note": "Массово"}).status_code == 200
    # …but never configures the platform or manages users.
    assert analyst.put("/api/settings/helmet_conf", json={"value": 0.9}).status_code == 403
    assert analyst.post("/api/cameras", json={"name": "X", "zone": "Y", "description": "", "rtsp_url": "rtsp://a/b", "fps_limit": 30, "enabled": True}).status_code == 403
    assert analyst.get("/api/auth/accounts").status_code == 403
    assert analyst.post("/api/auth/accounts", json={"login": "hacker", "label": "Hacker", "role": "admin", "password": "whatever"}).status_code == 403
    assert analyst.get("/api/models").status_code == 403

    guest = TestClient(main.app)
    assert guest.post("/api/auth/login", json={"login": "guest", "password": "guest-2026"}).status_code == 200
    assert guest.get("/api/dashboard").status_code == 200
    # Гость смотрит, но решения по событиям не принимает.
    assert guest.post(f"/api/events/{event_id}/ack", json={"note": "гость"}).status_code == 403


def test_admin_updates_deactivates_and_deletes_accounts(monkeypatch):
    enable_password_auth(monkeypatch)
    monkeypatch.setattr(main, "INITIAL_APP_PASSWORD", main.DEFAULT_INITIAL_APP_PASSWORD)
    with TestClient(main.app) as admin:
        assert admin.post("/api/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        assert admin.post("/api/auth/accounts", json={"login": "petrov", "label": "Петров", "role": "viewer", "password": "start-2026"}).status_code == 200

        worker = TestClient(main.app)
        assert worker.post("/api/auth/login", json={"login": "petrov", "password": "start-2026"}).status_code == 200
        assert worker.get("/api/dashboard").status_code == 200

        # Role change applies to the live session on the next request.
        assert admin.put("/api/auth/accounts/petrov", json={"role": "analyst"}).status_code == 200
        assert worker.get("/api/auth/status").json()["role"] == "analyst"

        # A new password ends the old sessions of that account only.
        assert admin.put("/api/auth/accounts/petrov", json={"password": "rotated-2026"}).status_code == 200
        assert worker.get("/api/dashboard").status_code == 401
        assert worker.post("/api/auth/login", json={"login": "petrov", "password": "rotated-2026"}).status_code == 200
        assert admin.get("/api/dashboard").status_code == 200

        # Disabling an account logs it out immediately.
        assert admin.put("/api/auth/accounts/petrov", json={"active": False}).status_code == 200
        assert worker.get("/api/dashboard").status_code == 401
        assert worker.post("/api/auth/login", json={"login": "petrov", "password": "rotated-2026"}).status_code == 401

        # Guard rails: the built-in admin stays, nobody deletes themselves.
        assert admin.delete("/api/auth/accounts/admin").status_code == 422
        assert admin.put("/api/auth/accounts/admin", json={"role": "viewer"}).status_code == 422
        assert admin.put("/api/auth/accounts/admin", json={"active": False}).status_code == 422
        assert admin.delete("/api/auth/accounts/petrov").status_code == 200
        assert admin.delete("/api/auth/accounts/petrov").status_code == 404
        assert {account["login"] for account in admin.get("/api/auth/accounts").json()["accounts"]} == {"admin", "director"}


def test_switching_accounts_in_one_browser_closes_the_previous_session(monkeypatch):
    """Signing in as somebody else hands the cookie over and ends the old session."""
    enable_password_auth(monkeypatch)
    monkeypatch.setattr(main, "INITIAL_APP_PASSWORD", main.DEFAULT_INITIAL_APP_PASSWORD)
    with TestClient(main.app) as browser:
        assert browser.post("/api/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        assert browser.post("/api/auth/accounts", json={"login": "smirnova", "label": "Смирнова", "role": "analyst", "password": "shift-2026"}).status_code == 200
        admin_session = browser.get("/api/auth/sessions").json()["sessions"][0]["id"]

        # Same browser, same cookie jar: the analyst replaces the admin.
        assert browser.post("/api/auth/login", json={"login": "smirnova", "password": "shift-2026"}).status_code == 200
        assert browser.get("/api/auth/status").json()["role"] == "analyst"
        active = {session["id"] for session in browser.get("/api/auth/sessions").json()["sessions"]}
        assert admin_session not in active
        assert len(active) == 1
