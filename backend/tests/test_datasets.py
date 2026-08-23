"""Dataset upload / validation / training-on-dataset job creation.

These are real: they build a YOLO-format zip on the fly and drive the API
to validate it, store it, and create a dataset-mode training job.
"""
import io
import zipfile

import pytest
from app import main
from fastapi.testclient import TestClient


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in entries.items():
            zf.writestr(path, data)
    return buf.getvalue()


def make_photos_zip() -> bytes:
    return _make_zip({f"photos/img_{i}.jpg": b"photo" for i in range(15)})


def make_videos_zip() -> bytes:
    return _make_zip({f"videos/clip_{i}.mp4": b"video-bytes" for i in range(3)})


def _make_dataset_zip() -> bytes:
    """Build a minimal valid YOLO detection dataset archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for cls in range(3):
            for i in range(6):
                zf.writestr(f"images/train/img_{cls}_{i}.jpg", b"fake-image")
                zf.writestr(f"labels/train/img_{cls}_{i}.txt", f"{cls} 0.5 0.5 0.2 0.2\n")
            for i in range(2):
                zf.writestr(f"images/val/img_{cls}_{i}.jpg", b"fake-image")
        zf.writestr("data.yaml",
                    "train: images/train\nval: images/val\nnames: {0: no_helmet, 1: no_vest, 2: smoking}\n")
        zf.writestr("README.txt", "sample\n")
    return buf.getvalue()


@pytest.fixture
def dataset_dir(tmp_path, monkeypatch):
    d = tmp_path / "datasets"
    monkeypatch.setattr(main, "DATASET_DIR", d)
    return d


def test_camera_capture_dataset_job_can_be_started_and_cancelled(dataset_dir):
    with TestClient(main.app) as c:
        started = c.post('/api/datasets/capture', json={
            'camera_id': 'cam_01', 'name': 'Camera Capture', 'image_count': 20, 'capture_fps': 2,
        })
        assert started.status_code == 202, started.text
        job = started.json()
        assert job['name'] == 'Camera_Capture' and job['target_count'] == 20
        listed = c.get('/api/datasets/capture/jobs').json()
        assert any(x['id'] == job['id'] for x in listed)
        cancelled = c.post(f"/api/datasets/capture/jobs/{job['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text


def test_upload_and_list_dataset(dataset_dir):
    with TestClient(main.app) as c:
        r = c.post("/api/datasets?name=My Dataset", content=_make_dataset_zip(),
                   headers={"Content-Type": "application/zip"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "My_Dataset"
        assert body["image_count"] == 24  # 3 classes x (6 train + 2 val)
        assert body["class_count"] == 3

        listed = c.get("/api/datasets").json()
        assert any(x["name"] == "My_Dataset" and x["exists"] for x in listed)


def test_upload_rejects_non_zip(dataset_dir):
    with TestClient(main.app) as c:
        r = c.post("/api/datasets?name=Bad", content=b"notazip",
                   headers={"Content-Type": "application/zip"})
        assert r.status_code == 422


def test_dataset_job_creation_uses_dataset_source(dataset_dir):
    with TestClient(main.app) as c:
        c.post("/api/datasets?name=SafeDS", content=_make_dataset_zip(),
               headers={"Content-Type": "application/zip"})
        r = c.post("/api/training/jobs", json={
            "camera_id": "", "source": "dataset", "dataset_name": "SafeDS",
            "epochs": 5, "image_count": 100,
        })
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["source"] == "dataset" and body["mode"] == "dataset"
        job = c.get(f"/api/training/jobs/{body['id']}").json()
        assert job["source"] == "dataset"
        assert job["dataset_name"] == "SafeDS"


def test_upload_plain_photos_auto_detects_images_kind(dataset_dir):
    with TestClient(main.app) as c:
        r = c.post("/api/datasets?name=MyPhotos", content=make_photos_zip(),
                   headers={"Content-Type": "application/zip"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "images"
        assert body["image_count"] == 15
        assert body["class_count"] == 0  # no labels; worker auto-labels


def test_upload_videos_auto_detects_videos_kind(dataset_dir):
    with TestClient(main.app) as c:
        r = c.post("/api/datasets?name=MyVideos", content=make_videos_zip(),
                   headers={"Content-Type": "application/zip"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "videos"
        assert body["media_count"] == 3
        assert body["class_count"] == 0


def test_preview_proxies_to_worker_and_returns_items(dataset_dir, monkeypatch):

    with TestClient(main.app) as c:
        c.post("/api/datasets?name=Px", content=make_photos_zip(),
               headers={"Content-Type": "application/zip"})
        monkeypatch.setattr(main, "TRAINING_WORKER_URL", "http://test-worker:8010")

        class _Resp:
            status_code = 200
            def json(self): return {"count": 2, "items": [{"source": "p0.jpg", "label": "1 объектов", "image": "AAAA"}]}
            def raise_for_status(self): return None
        monkeypatch.setattr(main.httpx, "post", lambda *a, **k: _Resp())
        r = c.post("/api/datasets/Px/preview", json={"limit": 3})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 2
        assert body["items"][0]["source"] == "p0.jpg"


def test_preview_requires_worker_and_degrades_honestly(dataset_dir):
    with TestClient(main.app) as c:
        c.post("/api/datasets?name=PreviewDS", content=make_photos_zip(),
               headers={"Content-Type": "application/zip"})
        # Without a configured training worker the preview must report an
        # honest 503, not fake a result.
        r = c.post("/api/datasets/PreviewDS/preview", json={"limit": 3})
        assert r.status_code == 503, r.text
        assert "worker" in r.json()["detail"].lower() or "training" in r.json()["detail"].lower()


def test_images_kind_job_routes_to_dataset_source(dataset_dir):
    with TestClient(main.app) as c:
        c.post("/api/datasets?name=PhotoDS", content=make_photos_zip(),
               headers={"Content-Type": "application/zip"})
        r = c.post("/api/training/jobs", json={
            "camera_id": "", "source": "dataset", "dataset_name": "PhotoDS", "epochs": 5,
        })
        assert r.status_code == 202, r.text
        assert r.json()["dataset_kind"] == "images"
