"""Camera-runtime tests without OpenCV, Torch or Ultralytics installed.

The RTSP state machine is intentionally tested with small fakes: CI verifies
that a camera can publish preview/telemetry without loading any ML package,
that failures do not block other streams, and that transport fallback works.
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
        self.open_args: tuple[object, object, object] | None = None
        self.released = False

    def open(self, url, backend, params):
        self.open_args = (url, backend, params)
        self._opened = type(self).default_opened
        return self._opened

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
    monkeypatch.setenv("CAMERA_DECODER", "opencv")
    monkeypatch.setenv("CAMERA_LIVE_FPS", "0")
    _FakeCap.default_opened = True
    _FakeCap.results = []

    cv2 = types.ModuleType("cv2")
    cv2.CAP_FFMPEG = 1900
    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC = 53
    cv2.CAP_PROP_READ_TIMEOUT_MSEC = 54
    cv2.CAP_PROP_BUFFERSIZE = 38
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.VideoCapture = _FakeCap
    cv2.resize = lambda image, size: image
    cv2.imencode = lambda suffix, image, params: (True, b"\xff\xd8frame\xff\xd9")
    monkeypatch.setitem(sys.modules, "cv2", cv2)

    # Intentionally do not stub torch/ultralytics. Camera bootstrap cannot
    # import either package until an active model exists.
    module_name = "zmk_inference_worker_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, WORKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(module_name, None)


def _camera(restart_requested_at: str = ""):
    return {
        "id": "cam_01",
        "name": "Test",
        "rtsp_url": "rtsp://user:secret@camera/stream",
        "fps_limit": 8,
        "restart_requested_at": restart_requested_at,
    }


def _record_internal(runtime):
    calls = []

    async def post_internal(path, data):
        calls.append((path, data))

    runtime.post_internal = post_internal
    return calls


def test_no_active_model_does_not_import_ml_dependencies(worker_mod):
    runtime = worker_mod.Runtime()

    async def get(path, internal=False):
        assert path == "/api/internal/active-model" and internal is True

    runtime.get = get
    asyncio.run(runtime.load_model())

    assert worker_mod._YOLO_CLASS is None
    assert runtime.model is None
    assert "torch" not in worker_mod.__dict__


def test_auto_transport_starts_with_tcp_then_falls_back_to_udp(worker_mod):
    worker_mod.RTSP_TRANSPORT = "auto"
    worker_mod.TRANSPORT_ORDER = ["tcp", "udp"]
    runtime = worker_mod.Runtime()

    assert runtime._next_transport("cam_x") == "tcp"
    assert runtime._next_transport("cam_x") == "udp"
    assert runtime._next_transport("cam_x") == "tcp"


def test_worker_builds_valid_ffmpeg_options(worker_mod):
    runtime = worker_mod.Runtime()
    capture = runtime._open_capture("rtsp://x/stream", "tcp")
    options = str(worker_mod.os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", ""))

    assert options.startswith("rtsp_transport;tcp"), options
    assert "|timeout;5000000" in options
    assert ",timeout" not in options and ",rtsp_transport" not in options
    assert capture.open_args == ("rtsp://x/stream", worker_mod.cv2.CAP_FFMPEG, [worker_mod.cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, worker_mod.cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000])


def test_failed_opens_eventually_mark_camera_offline(worker_mod):
    worker_mod.RECONNECT_MIN = 0
    _FakeCap.default_opened = False
    runtime = worker_mod.Runtime()
    calls = _record_internal(runtime)

    for _ in range(worker_mod.OFFLINE_AFTER):
        asyncio.run(runtime.frame(_camera()))

    statuses = [payload["status"] for path, payload in calls if path.endswith("/telemetry")]
    assert statuses[0] == "connecting"
    assert "recovering" in statuses
    assert statuses[-1] == "offline"


def test_waits_for_h264_keyframe_before_reconnecting(worker_mod):
    runtime = worker_mod.Runtime()
    _FakeCap.results = [(False, None), (False, None), (False, None), (True, _FakeImage())]
    calls = _record_internal(runtime)

    for _ in range(3):
        asyncio.run(runtime.frame(_camera()))

    session = runtime.sessions["cam_01"]
    assert session.capture is not None and session.capture.isOpened()
    assert session.failures == 0
    statuses = [payload["status"] for path, payload in calls if path.endswith("/telemetry")]
    assert "offline" not in statuses
    assert statuses[0] == "connecting"

    asyncio.run(runtime.frame(_camera()))
    assert runtime.sessions["cam_01"].received_first_frame is True


def test_reconnect_backoff_does_not_block_the_worker_loop(worker_mod):
    runtime = worker_mod.Runtime()
    config = worker_mod.CameraConfig.from_api(_camera())
    runtime.sessions[config.camera_id] = worker_mod.CameraSession(
        config=config,
        next_attempt_at=time.monotonic() + 5,
    )
    _record_internal(runtime)

    started = time.monotonic()
    asyncio.run(runtime.frame(config))

    assert time.monotonic() - started < 0.1


def test_live_preview_is_uploaded_without_an_active_model(worker_mod):
    runtime = worker_mod.Runtime()
    _FakeCap.results = [(True, _FakeImage())]
    calls = _record_internal(runtime)

    asyncio.run(runtime.frame(_camera()))

    snapshots = [data for path, data in calls if path.endswith("/snapshot")]
    assert len(snapshots) == 1
    assert snapshots[0]["jpeg_base64"]
    telemetry = [data for path, data in calls if path.endswith("/telemetry")]
    assert telemetry[-1]["status"] == "online"


def test_live_preview_posts_jpeg_at_configured_rate(worker_mod):
    worker_mod.LIVE_PREVIEW_FPS = 20
    runtime = worker_mod.Runtime()
    _FakeCap.results = [(True, _FakeImage())]
    posted = []

    async def post_internal(path, data):
        return None

    async def post_internal_jpeg(path, image):
        posted.append((path, image))

    runtime.post_internal = post_internal
    runtime.post_internal_jpeg = post_internal_jpeg
    asyncio.run(runtime.frame(_camera()))

    assert posted
    assert posted[0][0].endswith("/live-frame")
    assert posted[0][1].startswith(b"\xff\xd8")


def test_fps_is_measured_from_successful_frames(worker_mod):
    runtime = worker_mod.Runtime()
    config = worker_mod.CameraConfig.from_api(_camera())
    session = worker_mod.CameraSession(
        config=config,
        capture=_FakeCap(),
        status="online",
        telemetry_window_started=time.monotonic() - 10,
        last_telemetry_at=time.monotonic() - 10,
        frames_in_window=79,
    )
    runtime.sessions[config.camera_id] = session
    _FakeCap.results = [(True, _FakeImage())]
    calls = _record_internal(runtime)

    asyncio.run(runtime.frame(config))

    telemetry = [data for path, data in calls if path.endswith("/telemetry")]
    assert telemetry
    assert telemetry[-1]["fps"] == pytest.approx(8, rel=0.2)


def test_restart_token_releases_old_capture(worker_mod):
    runtime = worker_mod.Runtime()
    first = worker_mod.CameraConfig.from_api(_camera("one"))
    cap = _FakeCap()
    runtime.sessions[first.camera_id] = worker_mod.CameraSession(config=first, capture=cap)

    runtime._sync_cameras([_camera("two")])

    assert cap.released is True
    assert runtime.sessions[first.camera_id].capture is None
    assert runtime.sessions[first.camera_id].config.restart_token == "two"


def test_heartbeat_is_internal_and_has_no_rtsp_secret(worker_mod):
    runtime = worker_mod.Runtime()
    calls = _record_internal(runtime)
    config = worker_mod.CameraConfig.from_api(_camera())
    runtime.sessions[config.camera_id] = worker_mod.CameraSession(config=config)

    asyncio.run(runtime._heartbeat())

    assert calls[0][0] == "/api/internal/inference/heartbeat"
    assert "secret" not in calls[0][1]["detail"]
    assert calls[0][1]["camera_count"] == 1


def test_camera_errors_redact_rtsp_credentials(worker_mod):
    message = worker_mod.redact_error("could not open rtsp://user:password@camera/stream")
    assert "password" not in message
    assert "<rtsp-url>" in message


def test_ffmpeg_decoder_uses_corruption_tolerant_pipeline():
    source = WORKER.read_text(encoding="utf-8")
    for option in ("CAMERA_DECODER", "discardcorrupt", "ignore_err", "max_delay", "image2pipe", "ZMK_RTSP_URL"):
        assert option in source


def test_compose_forwards_all_camera_runtime_settings():
    import yaml

    compose = yaml.safe_load((WORKER.parents[2] / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"]["inference-worker"]["environment"]
    api_env = compose["services"]["api"]["environment"]
    assert "ZMK_WORKER_TOKEN_FILE" in api_env
    for variable in (
        "ZMK_WORKER_TOKEN_FILE",
        "CAMERA_DECODER",
        "RTSP_TRANSPORT",
        "RTSP_BUFFER_SIZE",
        "RTSP_STIMEOUT",
        "RTSP_TIMEOUT_OPTION",
        "RTSP_OPEN_TIMEOUT_MS",
        "RTSP_READ_TIMEOUT_MS",
        "RTSP_KEYFRAME_GRACE_SECONDS",
        "OFFLINE_AFTER_FRAMES",
        "RTSP_RECONNECT_SECONDS",
        "CAMERA_CONTROL_POLL_SECONDS",
        "CAMERA_TELEMETRY_SECONDS",
        "CAMERA_SNAPSHOT_SECONDS",
        "CAMERA_LIVE_FPS",
        "CAMERA_HEARTBEAT_SECONDS",
    ):
        assert variable in env
