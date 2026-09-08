"""Сводка по аналитикам: кто принял / не принял события и сколько работал.

Every review records its actor (`reviewed_by`), so the director and the
administrator can see a compact team digest: accepted, rejected and active
work time per analyst over the chosen period.
"""
from __future__ import annotations

from app import main
from fastapi.testclient import TestClient


def enable_password_auth(monkeypatch):
    monkeypatch.setattr(main, "PASSWORD_AUTH_ENABLED", True)
    monkeypatch.setattr(main, "INITIAL_APP_PASSWORD", "1234")
    main._auth_attempts.clear()


def _login(client, login, password):
    main._auth_attempts.clear()
    return client.post("/api/auth/login", json={"login": login, "password": password})


def test_analyst_summary_counts_reviews_and_work_time(monkeypatch):
    enable_password_auth(monkeypatch)
    with TestClient(main.app) as client:
        assert _login(client, "admin", "1234").status_code == 200
        created = client.post("/api/admin/users", json={
            "name": "Анна Аналитик", "login": "anna", "role": "analyst", "password": "anna-pass",
        })
        assert created.status_code == 201

        # Аналитик работает с очередью событий.
        assert _login(client, "anna", "anna-pass").status_code == 200
        events = client.get("/api/events?limit=10").json()
        pending = [event for event in events if event["review_status"] == "pending"]
        assert len(pending) >= 3
        accepted = client.post(f"/api/events/{pending[0]['id']}/ack", json={"note": "ок"})
        assert accepted.status_code == 200
        accepted2 = client.post(f"/api/events/{pending[1]['id']}/ack", json={"note": "проверено"})
        assert accepted2.status_code == 200
        rejected = client.post(f"/api/events/{pending[2]['id']}/reject", json={"note": "ложное"})
        assert rejected.status_code == 200

        # Администратор смотрит сводку за 24 часа.
        assert _login(client, "admin", "1234").status_code == 200
        summary = client.get("/api/reports/analysts?hours=24")
        assert summary.status_code == 200, summary.text
        body = summary.json()
        anna = next(item for item in body["analysts"] if item["login"] == "anna")
        assert anna["accepted"] == 2
        assert anna["rejected"] == 1
        assert anna["total"] == 3
        # Время работы — сумма промежутков между соседними проверками. Все три
        # проверки сделаны одним пакетом, поэтому окно работы крошечное; точное
        # равенство first_at == last_at гоняло тест по таймингу секунд.
        assert anna["work_seconds"] >= 0 and anna["work_seconds"] <= 5
        assert anna["first_at"] and anna["last_at"] and anna["first_at"] <= anna["last_at"]
        assert body["totals"]["accepted"] >= 2 and body["totals"]["rejected"] >= 1
        assert body["totals"]["active_analysts"] >= 1

        # Директор (гость) тоже видит сводку — только чтение.
        director = TestClient(main.app)
        assert _login(director, "director", "director").status_code == 200
        view = director.get("/api/reports/analysts?hours=24")
        assert view.status_code == 200
        assert any(item["login"] == "anna" for item in view.json()["analysts"])

        # Сам аналитик сводку не читает — она для директора и администратора.
        assert _login(client, "anna", "anna-pass").status_code == 200
        forbidden = client.get("/api/reports/analysts?hours=24")
        assert forbidden.status_code == 403

        # Короткий период без решений всё равно возвращает аккаунты с нулями.
        assert _login(client, "admin", "1234").status_code == 200
        empty = client.get("/api/reports/analysts?hours=1&start=2020-01-01T00:00:00&end=2020-01-01T01:00:00")
        assert empty.status_code == 200
        zero = next(item for item in empty.json()["analysts"] if item["login"] == "anna")
        assert zero["accepted"] == 0 and zero["rejected"] == 0
