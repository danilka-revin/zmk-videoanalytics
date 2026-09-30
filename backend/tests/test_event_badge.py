"""Бейдж вкладки «События» работает как бейдж «Логов».

Любая произошедшая детекция — не только критическая — поднимает число в меню.
Решение оператора («Принять» или «Не принять») убирает событие из счётчика, а
когда очередь разобрана, бейдж исчезает совсем: ровно то поведение, которое
оператор видит у счётчика ошибок вкладки «Логи».
"""

from app import main
from fastapi.testclient import TestClient


def _clear_events():
    con = main.db()
    con.execute("DELETE FROM events")
    con.commit()
    con.close()


def _insert(severity: str, review_status: str = "pending") -> int:
    reviewed = review_status != "pending"
    con = main.db()
    cursor = con.execute(
        "INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,acknowledged,review_status,note) VALUES(?,?,?,?,?,?,?,?,?)",
        (main.now_iso(), "cam_01", "no_helmet", severity, .9, "P-1", 1 if reviewed else 0, review_status, ""),
    )
    event_id = int(cursor.lastrowid)
    con.commit()
    con.close()
    return event_id


def test_badge_counts_every_new_event_not_only_critical():
    with TestClient(main.app) as c:
        _clear_events()
        _insert("medium")
        _insert("high")
        _insert("critical")
        dashboard = c.get("/api/dashboard").json()
        assert dashboard["pending_events"] == 3
        # Критические по-прежнему считаются отдельно для сводок и Telegram.
        assert dashboard["critical_unacked"] == 1


def test_badge_disappears_when_the_queue_is_reviewed():
    with TestClient(main.app) as c:
        _clear_events()
        accepted = _insert("high")
        rejected = _insert("medium")
        assert c.get("/api/dashboard").json()["pending_events"] == 2
        assert c.post(f"/api/events/{accepted}/ack", json={"note": "Проверено сменным мастером"}).status_code == 200
        assert c.get("/api/dashboard").json()["pending_events"] == 1
        assert c.post(f"/api/events/{rejected}/reject", json={"note": "Ложное срабатывание"}).status_code == 200
        assert c.get("/api/dashboard").json()["pending_events"] == 0


def test_bulk_review_removes_events_from_the_badge():
    with TestClient(main.app) as c:
        _clear_events()
        ids = [_insert("low") for _ in range(3)]
        assert c.get("/api/dashboard").json()["pending_events"] == 3
        assert c.post("/api/events/reject-bulk", json={"event_ids": ids, "note": "Ложные срабатывания"}).status_code == 200
        assert c.get("/api/dashboard").json()["pending_events"] == 0
        ids = [_insert("medium") for _ in range(2)]
        assert c.post("/api/events/ack-bulk", json={"event_ids": ids, "note": "Проверено"}).status_code == 200
        assert c.get("/api/dashboard").json()["pending_events"] == 0


def test_reviewed_events_stay_out_of_the_badge():
    with TestClient(main.app) as c:
        _clear_events()
        _insert("critical", "accepted")
        _insert("critical", "rejected")
        dashboard = c.get("/api/dashboard").json()
        assert dashboard["pending_events"] == 0
        assert dashboard["critical_unacked"] == 0
