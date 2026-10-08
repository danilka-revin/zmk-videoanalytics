"""Operator event exports keep Russian labels and the available evidence frames."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timedelta

from app import main
from fastapi.testclient import TestClient


def test_russian_event_csv_and_evidence_zip_keep_full_event_context():
    with TestClient(main.app) as client:
        con = main.db()
        cursor = con.execute(
            """INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,external_id,
               acknowledged,review_status,reviewed_at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                main.now_iso(), "cam_01", "no_helmet", "critical", 0.9342,
                "worker-17", "REPORT-EVIDENCE-001", 1, "accepted", main.now_iso(), "=Проверено оператором",
            ),
        )
        event_id = int(cursor.lastrowid)
        con.commit()
        con.close()

        evidence = main.event_frame_path_for(event_id)
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_bytes(b"\xff\xd8annotated-event-frame\xff\xd9")

        table = client.get("/api/reports/events.csv?q=REPORT-EVIDENCE-001")
        assert table.status_code == 200, table.text
        assert "zmk-events-ru.csv" in table.headers["content-disposition"]
        text = table.content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
        assert len(rows) == 1
        row = rows[0]
        assert {"№ события", "Тип нарушения", "Камера", "Зона", "Комментарий оператора", "Кадр нарушения", "Файл кадра"} <= set(row)
        assert row["Тип нарушения"] == "Без каски"
        assert row["Критичность"] == "Критический"
        assert row["Камера"] == "Камера 01"
        assert row["Кадр нарушения"] == "Есть"
        assert row["Файл кадра"] == f"frames/event-{event_id}.jpg"
        # Spreadsheet formula characters are still neutralised in the CSV.
        assert row["Комментарий оператора"] == "'=Проверено оператором"

        archive_response = client.get("/api/reports/events.zip?q=REPORT-EVIDENCE-001")
        assert archive_response.status_code == 200, archive_response.text
        assert archive_response.headers["content-type"].startswith("application/zip")
        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
            names = set(archive.namelist())
            assert {"events_ru.csv", "report.html", "README.txt", "manifest.json", f"frames/event-{event_id}.jpg"} <= names
            assert archive.read(f"frames/event-{event_id}.jpg") == evidence.read_bytes()
            html = archive.read("report.html").decode("utf-8")
            assert f'<img src="frames/event-{event_id}.jpg"' in html
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["events"] == 1 and manifest["frames"] == 1


def test_event_evidence_zip_honors_overview_period():
    with TestClient(main.app) as client:
        con = main.db()
        con.execute(
            "INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,external_id,acknowledged,note) VALUES(?,?,?,?,?,?,?,?,?)",
            ((datetime.now(main.TZ)-timedelta(hours=48)).isoformat(), "cam_01", "smoking", "high", 0.9, "worker-old", "REPORT-OLDER-THAN-OVERVIEW", 0, ""),
        )
        con.commit()
        con.close()

        archive_response = client.get("/api/reports/events.zip?hours=24&q=REPORT-OLDER-THAN-OVERVIEW")
        assert archive_response.status_code == 200, archive_response.text
        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["events"] == 0


def _insert_event(person_id: str, zone: str = "Тестовая зона") -> int:
    con = main.db()
    cursor = con.execute(
        """INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,external_id,
           acknowledged,review_status,reviewed_at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            main.now_iso(), "cam_01", "no_helmet", "critical", 0.91,
            person_id, f"{person_id}-ID", 1, "accepted", main.now_iso(), "Проверено",
        ),
    )
    event_id = int(cursor.lastrowid)
    con.commit()
    con.close()
    evidence = main.event_frame_path_for(event_id)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_bytes(b"\xff\xd8brief-export-frame\xff\xd9")
    return event_id


def test_short_export_keeps_only_time_place_violator_and_photo():
    """Директору нужен минимальный отчёт: когда, где, кто нарушил, есть ли фото."""
    with TestClient(main.app) as client:
        event_id = _insert_event("SHORT-EXPORT-001")

        table = client.get("/api/reports/events.csv?columns=short&q=SHORT-EXPORT-001")
        assert table.status_code == 200, table.text
        text = table.content.decode("utf-8-sig")
        header = text.splitlines()[0].split(";")
        assert header == ["№ события", "Дата и время", "Тип нарушения", "Место", "Кто нарушил", "Фото", "Файл кадра"]

        row = next(iter(csv.DictReader(io.StringIO(text), delimiter=";")))
        assert row["Тип нарушения"] == "Без каски"
        assert row["Место"] == "Камера 01 · Тестовая зона"
        assert row["Кто нарушил"] == "SHORT-EXPORT-001"
        assert row["Фото"] == "Есть"
        assert row["Файл кадра"] == f"frames/event-{event_id}.jpg"
        # Полной карточки в коротком отчёте нет: она остаётся у администратора.
        assert "Комментарий оператора" not in row and "Уверенность, %" not in row

        archive_response = client.get("/api/reports/events.zip?columns=short&q=SHORT-EXPORT-001")
        assert archive_response.status_code == 200, archive_response.text
        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["columns"] == ["index", "time", "type", "place", "person", "photo", "frame_file"]
            # Кадры кладутся в архив независимо от набора колонок: фото должно быть.
            assert f"frames/event-{event_id}.jpg" in archive.namelist()
            html = archive.read("report.html").decode("utf-8")
            assert f'<img src="frames/event-{event_id}.jpg"' in html
            assert "Комментарий оператора" not in html


def test_explicit_column_list_and_unknown_columns():
    with TestClient(main.app) as client:
        _insert_event("COLUMN-LIST-001")

        response = client.get("/api/reports/events.csv?columns=time,place,person,photo,nonsense&q=COLUMN-LIST-001")
        assert response.status_code == 200, response.text
        header = response.content.decode("utf-8-sig").splitlines()[0].split(";")
        assert header == ["Дата и время", "Место", "Кто нарушил", "Фото"]

        # Неизвестный или пустой набор не ломает отчёт: возвращается полная таблица.
        fallback = client.get("/api/reports/events.csv?columns=unknown-only&q=COLUMN-LIST-001")
        assert fallback.status_code == 200, fallback.text
        assert "Комментарий оператора" in fallback.content.decode("utf-8-sig").splitlines()[0]

        default_response = client.get("/api/reports/events.csv?q=COLUMN-LIST-001")
        assert "Кадр нарушения" in default_response.content.decode("utf-8-sig").splitlines()[0]
