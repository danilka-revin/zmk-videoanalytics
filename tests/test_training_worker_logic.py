"""Tests the training worker's media-prep logic (photos pack and video pack)
without the heavy ultralytics/torch runtime: those are stubbed with light
fakes so we can import the module and exercise pure file ops.

Verified pieces:
  - _gather_images / _gather_videos pick the right files from a folder pack
  - _extract_frames reads a real (tiny) video via OpenCV and produces frames
  - _finalize_dataset splits labelled frames into train/val and writes data.yaml
"""
import sys
import types
from pathlib import Path

import pytest

# opencv is a heavy runtime dependency that is only guaranteed inside the
# training_worker container image; skip these tests when it is unavailable
# (e.g. the bare CI python env).
cv2 = pytest.importorskip("cv2")
import numpy as np

WORKER = Path(__file__).resolve().parents[1] / "services" / "training_worker"


@pytest.fixture(scope="module", autouse=True)
def stub_heavier_deps():
    """Provide minimal ultralytics/torch stubs so the worker module imports."""
    ultralytics = types.ModuleType("ultralytics")
    ultralytics.__path__ = []

    class _StubYOLO:
        def __init__(self, *a, **k):
            self.names = {0: "no_helmet", 1: "no_vest"}

        def __call__(self, *a, **k):
            return self

        def predict(self, source, **k):
            return [_FakeResult()]

    class _FakeResult:
        orig_shape = (640, 640)
        boxes = types.SimpleNamespace(
            xyxy=np.array([[10, 10, 100, 200]], dtype=np.float32).reshape(-1, 4),
            cls=np.array([0], dtype=np.float32),
            conf=np.array([0.9], dtype=np.float32),
        )

    ultralytics.YOLO = _StubYOLO
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    sys.modules["ultralytics"] = ultralytics
    sys.modules["torch"] = torch

    sys.path.insert(0, str(Path(WORKER)))
    yield


def test_worker_module_imports_and_gathers_media(tmp_path):
    import main as worker

    # photos pack
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "a.jpg").write_bytes(b"x")
    (photos / "b.png").write_bytes(b"x")
    (photos / "notes.txt").write_text("x")
    imgs = worker._gather_images(photos)
    assert {p.name for p in imgs} == {"a.jpg", "b.png"}

    # videos pack
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "clip1.mp4").write_bytes(b"x")
    (videos / "clip2.AVI").write_bytes(b"x")
    (videos / "s.json").write_text("{}")
    vids = worker._gather_videos(videos)
    assert {p.name for p in vids} == {"clip1.mp4", "clip2.AVI"}


def test_training_prefers_active_pytorch_ppe_weights_when_available(tmp_path):
    import main as worker

    ppe = tmp_path / "ppe-person-helmet.pt"
    ppe.write_bytes(b"weights")
    assert worker._training_base_model(types.SimpleNamespace(base_artifact=f"file://{ppe}")) == str(ppe)
    # An ONNX inference artifact is not resumable by the Ultralytics trainer.
    assert worker._training_base_model(types.SimpleNamespace(base_artifact=f"file://{tmp_path / 'ppe.onnx'}")) == worker.BASE_MODEL


def test_extract_frames_from_real_video(tmp_path):
    import main as worker

    # Build a real 20-frame video with OpenCV.
    video = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
    for i in range(20):
        writer.write(np.zeros((64, 64, 3), dtype=np.uint8))
    writer.release()

    frames = worker._extract_frames([video], Path(tmp_path) / "out", frame_skip=5)
    # ~20 frames / skip 5 => roughly 4 frames
    assert len(frames) >= 3
    assert all(p.exists() for p in frames)


def test_preview_dataset_returns_annotated_frames(tmp_path):
    import main as worker

    photos = tmp_path / "photos"
    photos.mkdir()
    for i in range(3):
        img = np.zeros((120, 160, 3), dtype=np.uint8)
        cv2.imwrite(str(photos / f"p{i}.jpg"), img)

    req = worker.PreviewRequest(dataset_path=str(photos), base_artifact=None,
                                confidence=0.35, limit=3, kind="images")
    out = worker.preview_dataset(req)
    assert out["count"] >= 1
    assert "image" in out["items"][0]
    assert out["items"][0]["source"].endswith(".jpg")
    # the returned payload decodes back to a real JPEG
    import base64
    data = base64.b64decode(out["items"][0]["image"])
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"


def test_finalize_dataset_writes_data_yaml_and_splits(tmp_path):
    import main as worker

    work = Path(tmp_path) / "job"
    (work / "images" / "all").mkdir(parents=True)
    (work / "labels" / "all").mkdir(parents=True)
    names = {0: "no_helmet", 1: "no_vest"}
    for i in range(12):
        (work / "images" / "all" / f"{i:06}.jpg").write_bytes(b"img")
        (work / "labels" / "all" / f"{i:06}.txt").write_text("0 0.5 0.5 0.2 0.2\n")

    data_yaml = worker._finalize_dataset(work, names, val_split=0.2)
    assert data_yaml.exists()
    text = data_yaml.read_text()
    assert "images/train" in text and "images/val" in text and "no_helmet" in text
    # train+val must cover all labelled frames
    n_train = len(list((work / "images" / "train").glob("*")))
    n_val = len(list((work / "images" / "val").glob("*")))
    assert n_train + n_val == 12
    assert n_val >= 1
