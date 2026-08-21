import asyncio
from pathlib import Path

import pytest
from app import main


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every test gets a fresh DB; fixtures never exist in production code or preview."""
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "videoanalytics-test.db")
    monkeypatch.setattr(main, "SEED_TEST_DATA", True)
    main._rate_buckets.clear()
    original_init = main.init_db

    def init_with_fixtures():
        original_init()
        if not main.SEED_TEST_DATA:
            return
        con = main.db()
        timestamp = main.now_iso()
        cameras = [
            (f"cam_{i:02}", f"Камера {i:02}", "Тестовая зона", "Fixture only", f"rtsp://camera-{i:02}/stream", 8, "offline" if i == 7 else "online", 8, 150, 1, timestamp, timestamp)
            for i in range(1, 11)
        ]
        con.executemany("INSERT INTO cameras(id,name,zone,description,rtsp_url,fps_limit,status,fps,latency_ms,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", cameras)
        for i in range(24):
            con.execute("INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,acknowledged,note) VALUES(?,?,?,?,?,?,?,?)", (timestamp, f"cam_{i % 10 + 1:02}", "no_helmet", "critical" if i % 4 == 0 else "high", .92, f"P-{i}", 0, ""))
        con.executemany("INSERT INTO users(name,login,role,active,created_at) VALUES(?,?,?,?,?)", [("Test Admin", "admin", "admin", 1, timestamp), ("Test Operator", "operator", "operator", 1, timestamp), ("Test Viewer", "viewer", "viewer", 1, timestamp)])
        con.executemany("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)", [("siz-guard-v2.1", "TensorRT FP16", "ready", 92.4, 87.1, timestamp, "fixture", "file:///test/v21.engine", ""), ("siz-guard-v2.0", "ONNX FP32", "ready", 90.8, 85.9, timestamp, "fixture", "file:///test/v20.onnx", "")])
        con.execute("UPDATE settings SET value='siz-guard-v2.1' WHERE key='active_model'")
        con.commit(); con.close()

    async def fake_training_worker(job_id: int):
        await asyncio.sleep(.1)
        con = main.db(); job = con.execute("SELECT target_name,camera_id FROM training_jobs WHERE id=?", (job_id,)).fetchone()
        con.execute("UPDATE training_jobs SET status='completed',progress=100,stage='Fixture complete',updated_at=? WHERE id=?", (main.now_iso(), job_id))
        con.execute("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)", (job[0], "ONNX FP16", "ready", 93.0, 88.0, main.now_iso(), f"fixture:{job[1]}", f"file:///test/{job[0]}.onnx", ""))
        con.commit(); con.close()

    monkeypatch.setattr(main, "init_db", init_with_fixtures)
    monkeypatch.setattr(main, "run_training", fake_training_worker)
    yield
