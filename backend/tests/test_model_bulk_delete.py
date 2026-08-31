"""Bulk model deletion keeps per-model outcomes explicit and safe."""
from __future__ import annotations

from pathlib import Path

import pytest
from app import main
from fastapi.testclient import TestClient


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    directory = tmp_path / "models"
    monkeypatch.setattr(main, "MODEL_DIR", directory)
    return directory


def upload(client: TestClient, name: str) -> None:
    result = client.post(
        "/api/models/upload",
        params={"name": name, "format": "ONNX", "precision": "94", "recall": "90", "filename": f"{name}.onnx"},
        content=f"artifact:{name}".encode(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert result.status_code == 201, result.text


def test_bulk_delete_removes_selected_models_and_stops_active_one(model_dir: Path):
    with TestClient(main.app) as client:
        upload(client, "bulk-alpha")
        upload(client, "bulk-beta")
        assert client.post("/api/models/bulk-alpha/activate").status_code == 200

        result = client.post("/api/models/delete-bulk", json={
            "names": ["bulk-alpha", "missing-model", "bulk-beta"],
            "deactivate_active": True,
        })
        assert result.status_code == 200, result.text
        body = result.json()
        assert {item["name"] for item in body["deleted"]} == {"bulk-alpha", "bulk-beta"}
        assert body["failed"] == [{"name": "missing-model", "status": 404, "detail": "Модель не найдена"}]
        assert not (model_dir / "bulk-alpha.onnx").exists()
        assert not (model_dir / "bulk-beta.onnx").exists()
        remaining = {item["name"] for item in client.get("/api/models").json()}
        assert "bulk-alpha" not in remaining and "bulk-beta" not in remaining


def test_bulk_delete_keeps_active_model_when_deactivation_not_confirmed(model_dir: Path):
    with TestClient(main.app) as client:
        upload(client, "bulk-active")
        assert client.post("/api/models/bulk-active/activate").status_code == 200
        result = client.post("/api/models/delete-bulk", json={"names": ["bulk-active"], "deactivate_active": False})

    assert result.status_code == 200
    assert result.json()["deleted"] == []
    assert result.json()["failed"] == [{
        "name": "bulk-active", "status": 409,
        "detail": "Модель активна. Сначала переключитесь на другую или подтвердите остановку и удаление.",
    }]
    assert (model_dir / "bulk-active.onnx").is_file()
