"""Security hardening tests: path traversal on snapshot paths and
decompression-bomb (zip) protection on dataset upload.
"""
import io
import zipfile

from app import main
from fastapi.testclient import TestClient


def test_snapshot_path_rejects_traversal():
    # A crafted camera_id must never escape the snapshots directory.
    with TestClient(main.app) as c:
        for bad in ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "a/b", "a.b", "cam/../x"]:
            r = c.get(f"/api/cameras/{bad}/snapshot")
            # Either 400 (format/path rejected) or 404 (no such camera) — never a file read.
            assert r.status_code in (400, 404), (bad, r.status_code)
            assert "image/jpeg" not in r.headers.get("content-type", "")


def test_upload_requires_valid_snapshot_path():
    with TestClient(main.app) as c:
        r = c.post("/api/cameras/../../x/snapshot", json={
            "jpeg_base64": "AAAA", "captured_at": "2026-01-01T00:00:00+00:00",
        })
        assert r.status_code in (400, 404, 422)


def test_dataset_zip_too_many_files_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(20000):  # exceeds the 15000 member cap
            zf.writestr(f"images/train/x_{i}.jpg", b"x")
    with TestClient(main.app) as c:
        r = c.post("/api/datasets?name=bomb", content=buf.getvalue(),
                   headers={"Content-Type": "application/zip"})
        assert r.status_code == 413, r.text


def test_dataset_zip_symlink_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("data.yaml")
        info.external_attr = (0o120777 << 16)  # symlink
        zf.writestr(info, b"names: [a]")
    with TestClient(main.app) as c:
        r = c.post("/api/datasets?name=link", content=buf.getvalue(),
                   headers={"Content-Type": "application/zip"})
        assert r.status_code == 422, r.text
