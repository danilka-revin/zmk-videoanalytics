"""Local model artifact uploads are streamed, registered and safely cleaned up."""
from __future__ import annotations

import hashlib

import pytest
from app import main
from fastapi.testclient import TestClient


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    directory = tmp_path / "shared-models"
    monkeypatch.setattr(main, "MODEL_DIR", directory)
    return directory


def upload_params(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "name": "local-ppe-v1",
        "format": "ONNX",
        "precision": "93.4",
        "recall": "88.2",
        "filename": "factory-ppe.onnx",
    }
    values.update(overrides)
    return values


def test_uploads_local_model_to_shared_volume_registers_checksum_and_deletes_it(model_dir):
    artifact = b"pretend-onnx-artifact\x00" * 64
    with TestClient(main.app) as client:
        result = client.post(
            "/api/models/upload",
            params=upload_params(filename="../../factory-ppe.onnx"),
            content=artifact,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert result.status_code == 201, result.text
        body = result.json()
        target = model_dir / "local-ppe-v1.onnx"
        assert body["uploaded"] is True
        assert body["artifact_uri"] == f"file://{target}"
        assert body["checksum"] == hashlib.sha256(artifact).hexdigest()
        assert body["source"] == "upload:factory-ppe.onnx"
        assert target.read_bytes() == artifact

        listed = client.get("/api/models").json()
        uploaded = next(item for item in listed if item["name"] == "local-ppe-v1")
        assert uploaded["artifact_uri"] == f"file://{target}"
        assert uploaded["checksum"] == hashlib.sha256(artifact).hexdigest()

        deleted = client.delete("/api/models/local-ppe-v1")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["removed_artifact_file"] is True
        assert not target.exists()


def test_activation_rejects_uploaded_model_when_shared_artifact_disappeared(model_dir):
    with TestClient(main.app) as client:
        uploaded = client.post(
            "/api/models/upload", params=upload_params(name="lost-artifact"), content=b"onnx-data",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert uploaded.status_code == 201, uploaded.text
        (model_dir / "lost-artifact.onnx").unlink()
        activated = client.post("/api/models/lost-artifact/activate")

    assert activated.status_code == 409
    assert "отсутствует" in activated.json()["detail"]


def test_rejects_extension_mismatch_without_creating_model_file(model_dir):
    with TestClient(main.app) as client:
        result = client.post(
            "/api/models/upload",
            params=upload_params(filename="weights.pt", format="ONNX"),
            content=b"not-an-onnx",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert result.status_code == 422
    assert ".onnx" in result.text
    assert not model_dir.exists()


def test_respects_streaming_size_limit_and_keeps_existing_artifact_safe(model_dir, monkeypatch):
    model_dir.mkdir(parents=True)
    existing = model_dir / "local-ppe-v1.onnx"
    existing.write_bytes(b"existing-artifact")
    monkeypatch.setattr(main, "MODEL_UPLOAD_MAX_BYTES", 5)

    def chunked_body():
        # No Content-Length: this exercises the endpoint's in-stream limit,
        # not just the middleware's early Content-Length guard.
        yield b"123"
        yield b"456"

    with TestClient(main.app) as client:
        too_large = client.post(
            "/api/models/upload",
            params=upload_params(name="too-large"),
            content=chunked_body(),
            headers={"Content-Type": "application/octet-stream"},
        )
        collision = client.post(
            "/api/models/upload",
            params=upload_params(),
            content=b"1234",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert too_large.status_code == 413
    assert collision.status_code == 409
    assert existing.read_bytes() == b"existing-artifact"
    assert not list(model_dir.glob("*.upload"))
