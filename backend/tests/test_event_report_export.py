"""Operator event exports keep Russian labels and the available evidence frames."""
from __future__ import annotations

import csv
import io
import json
import zipfile

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
