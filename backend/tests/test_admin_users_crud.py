"""Admin → Пользователи must manage real login-capable accounts.

The legacy `users` table rows could not be used for login (no password, no
account row). These tests lock the new contract: creation requires a password,
the created user can sign in, roles can be edited (гость → администратор),
and users can be disabled or deleted.
"""
from __future__ import annotations

from app import main
from fastapi.testclient import TestClient


def enable_password_auth(monkeypatch):
    monkeypatch.setattr(main, "PASSWORD_AUTH_ENABLED", True)
    monkeypatch.setattr(main, "INITIAL_APP_PASSWORD", "1234")
    main._auth_attempts.clear()


def _login(client, login, password):
    # The in-memory limiter is per source; tests sign in many times on purpose.
    main._auth_attempts.clear()
    return client.post("/api/auth/login", json={"login": login, "password": password})


def test_admin_users_crud_and_real_login(monkeypatch):
    enable_password_auth(monkeypatch)
    with TestClient(main.app) as client:
        assert _login(client, "admin", "1234").status_code == 200
        # Switching accounts in the same browser closes the previous session, so
        # after every user check the admin signs back in.
        def as_admin():
            assert _login(client, "admin", "1234").status_code == 200

        # 1. Admin creates a user with a password (the old silent create did not).
        created = client.post("/api/admin/users", json={
            "name": "Иван Сменный", "login": "ivanov", "role": "viewer", "password": "ivanov-pass",
        })
        assert created.status_code == 201, created.text
        account = created.json()["account"]
        assert account["login"] == "ivanov" and account["role"] == "viewer"

        # 2. The created account can actually sign in.
        sign_in = _login(client, "ivanov", "ivanov-pass")
        assert sign_in.status_code == 200
        assert sign_in.json()["role"] == "viewer"

        # 3. A viewer has no access to the user manager (guest cannot enter admin).
        assert client.get("/api/admin/users").status_code == 403
        assert client.post("/api/admin/users", json={
            "name": "x", "login": "x", "role": "viewer", "password": "xxxx",
        }).status_code == 403
        as_admin()

        # 4. The admin can promote the user to administrator, rename, reset the
        #    password and toggle the account — everything a user manager needs.
        promoted = client.put("/api/admin/users/ivanov", json={"name": "Иван Петров", "role": "admin"})
        assert promoted.status_code == 200
        assert promoted.json()["account"]["role"] == "admin"
        assert _login(client, "ivanov", "ivanov-pass").status_code == 200
        as_admin()

        reset = client.put("/api/admin/users/ivanov", json={"password": "board-pass"})
        assert reset.status_code == 200
        assert _login(client, "ivanov", "ivanov-pass").status_code == 401
        assert _login(client, "ivanov", "board-pass").status_code == 200
        as_admin()

        listed = client.get("/api/admin/users").json()
        assert {a["login"] for a in listed["accounts"]} >= {"admin", "ivanov"}

        disabled = client.patch("/api/admin/users/ivanov/toggle")
        assert disabled.status_code == 200 and disabled.json()["active"] is False
        assert _login(client, "ivanov", "board-pass").status_code == 401
        as_admin()
        enabled = client.patch("/api/admin/users/ivanov/toggle")
        assert enabled.status_code == 200 and enabled.json()["active"] is True
        assert _login(client, "ivanov", "board-pass").status_code == 200
        as_admin()

        # 5. The administrator can delete the user; sessions are revoked.
        deleted = client.delete("/api/admin/users/ivanov")
        assert deleted.status_code == 200
        assert _login(client, "ivanov", "board-pass").status_code == 401
        as_admin()

        # The built-in administrator stays protected.
        assert client.delete("/api/admin/users/admin").status_code == 422
        assert client.patch("/api/admin/users/admin/toggle").status_code == 422
        assert client.get("/api/admin/summary").json()["users"] >= 1
