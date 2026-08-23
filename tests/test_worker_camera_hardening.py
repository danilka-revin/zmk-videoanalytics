"""Regression tests for the RTSP inference worker.

The production worker depends on OpenCV, Torch and Ultralytics, which are
container-only dependencies. Lightweight module stubs let CI execute the real
camera state-machine instead of skipping the most important regressions.
"""
import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path
from typing import ClassVar

import pytest

WORKER = Path(__file__).resolve().parents[1] / "services" / "inference_worker" / "main.py"


class _FakeImage:
    shape = (20, 20, 3)


class _FakeCap:
    default_opened: ClassVar[bool] = True
    results: ClassVar[list[tuple[bool, object | None]]] = []

    def __init__(self, *args, **kwargs):
        self._opened = type(self).default_opened
        self.options: list[tuple[object, object]] = []
        self.released = False

    def isOpened(self):
        return self._opened

    def read(self):
        if not type(self).results:
            return False, None
        return type(self).results.pop(0)

    def set(self, key, value):
        self.options.append((key, value))
        return True

    def release(self):
        self._opened = False
        self.released = True


@pytest.fixture
def worker_mod(monkeypatch):
    monkeypatch.setenv("RTSP_TRANSPORT", "tcp")
    monkeypatch.setenv("RTSP_STIMEOUT", "5000000")
    _FakeCap.default_opened = True
    _FakeCap.results = []

    cv2 = types.ModuleType("cv2")
    cv2.CAP_FFMPEG = 1900
    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC = 53
    cv2.CAP_PROP_READ_TIMEOUT_MSEC = 54
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.VideoCapture = _FakeCap
    cv2.resize = lambda image, size: image
    cv2.imencode = lambda suffix, image, params: (True, b"\xff\xd8frame\xff\xd9")

    class _StubYOLO:
        names: ClassVar[dict] = {0: "no_helmet"}

        def __init__(self, *args, **kwargs):
            pass

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    ultralytics = types.ModuleType("ultralytics")
    ultralytics.YOLO = _StubYOLO
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "ultralytics", ultralytics)

    module_name = "zmk_inference_worker_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, WORKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(module_name, None)


def _camera():
    return {
        "id": "cam_01",
        "name": "Test",
        "rtsp_url": "rtsp://x/stream",
        "fps_limit": 8,
    }


def test_offline_reported_only_after_threshold(worker_mod):
    worker = worker_mod
    runtime = worker.Runtime()
    runtime.model = None
    camera = _camera()
    _FakeCap.results = [(True, _FakeImage()), (True, _FakeImage())] + [(False, None)] * 12
    runtime.captures[camera["id"]] = _FakeCap()
    runtime.frame_counts[camera["id"]] = 0
    runtime.last_telemetry[camera["id"]] = time.time() - 11

    statuses = []

    async def post(path, data):
        if path.startswith("/api/cameras/cam_01/telemetry"):
            statuses.append(data["status"])

    runtime.post = post
    for _ in range(6):
        asyncio.run(runtime.frame(camera))

    assert statuses[0] == "online"
    assert "recovering" in statuses
    assert "offline" in statuses


def test_auto_transport_starts_with_tcp_then_falls_back_to_udp(worker_mod):
    worker_mod.RTSP_TRANSPORT = "auto"
    worker_mod.TRANSPORT_ORDER = ["tcp", "udp"]
    runtime = worker_mod.Runtime()

    assert runtime._next_transport("cam_x") == "tcp"
    assert runtime._next_transport("cam_x") == "udp"
    assert runtime._next_transport("cam_x") == "tcp"

    worker_mod.RTSP_TRANSPORT = "tcp"
    worker_mod.TRANSPORT_ORDER = ["tcp"]
    assert runtime._next_transport("cam_y") == "tcp"


def test_worker_builds_valid_ffmpeg_options(worker_mod):
    runtime = worker_mod.Runtime()
    runtime._open_capture("rtsp://x/stream", "tcp")
    options = str(worker_mod.os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", ""))

    assert options.startswith("rtsp_transport;tcp"), options
    assert "|stimeout;5000000" in options
    assert ",stimeout" not in options and ",rtsp_transport" not in options
    assert worker_mod.OFFLINE_AFTER >= 1


def test_failed_opens_eventually_mark_camera_offline(worker_mod):
    worker = worker_mod
    worker.RECONNECT_MIN = 0
    _FakeCap.default_opened = False
    runtime = worker.Runtime()
    statuses = []

    async def post(path, data):
        if path.endswith("/telemetry"):
            statuses.append(data["status"])

    runtime.post = post
    for _ in range(worker.OFFLINE_AFTER):
        asyncio.run(runtime.frame(_camera()))

    assert statuses[0] == "recovering"
    assert statuses[-1] == "offline"


def test_reconnect_backoff_does_not_block_the_worker_loop(worker_mod):
    runtime = worker_mod.Runtime()
    runtime.next_open["cam_01"] = time.time() + 5

    started = time.monotonic()
    asyncio.run(runtime.frame(_camera()))

    assert time.monotonic() - started < 0.1


def test_live_preview_is_uploaded_without_an_active_model(worker_mod):
    runtime = worker_mod.Runtime()
    runtime.model = None
    runtime.captures["cam_01"] = _FakeCap()
    _FakeCap.results = [(True, _FakeImage())]
    posted = []

    async def post(path, data):
        posted.append((path, data))

    runtime.post = post
    asyncio.run(runtime.frame(_camera()))

    snapshots = [data for path, data in posted if path.endswith("/snapshot")]
    assert len(snapshots) == 1
    assert snapshots[0]["jpeg_base64"]


def test_fps_is_measured_from_successful_frames(worker_mod):
    runtime = worker_mod.Runtime()
    runtime.model = None
    runtime.captures["cam_01"] = _FakeCap()
    _FakeCap.results = [(True, _FakeImage())]
    runtime.frame_counts["cam_01"] = 88
    runtime.last_telemetry["cam_01"] = time.time() - 11
    runtime.last_status["cam_01"] = "online"
    telemetry = []

    async def post(path, data):
        if path.endswith("/telemetry"):
            telemetry.append(data)

    runtime.post = post
    asyncio.run(runtime.frame(_camera()))

    assert telemetry
    assert telemetry[0]["fps"] > 0
    assert telemetry[0]["fps"] == pytest.approx(8, rel=0.15)


def test_compose_forwards_all_rtsp_settings():
    import yaml

    compose = yaml.safe_load((WORKER.parents[2] / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"]["inference-worker"]["environment"]
    api_env = compose["services"]["api"]["environment"]
    assert "ZMK_WORKER_TOKEN_FILE" in api_env
    for variable in (
        "ZMK_WORKER_TOKEN_FILE",
        "RTSP_TRANSPORT",
        "RTSP_BUFFER_SIZE",
        "RTSP_STIMEOUT",
        "OFFLINE_AFTER_FRAMES",
        "RTSP_RECONNECT_SECONDS",
    ):
        assert variable in env
