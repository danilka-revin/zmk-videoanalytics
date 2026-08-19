from pathlib import Path

import pytest
from app import main


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every test gets a fresh DB and can never mutate the preview/production database."""
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "videoanalytics-test.db")
    main._rate_buckets.clear()
    yield
