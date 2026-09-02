"""Camera-runtime tests without OpenCV, Torch or Ultralytics installed.

The RTSP state machine is intentionally tested with small fakes: CI verifies
that a camera can publish preview/telemetry without loading any ML package,
that failures do not block other streams, and that transport fallback works.
"""
import asyncio
import importlib.util
import io
import sys
import time
import types
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

WORKER = Path(__file__).resolve().parents[1] / "services" / "inference_worker" / "main.py"


class _FakeImage:
    shape = (20, 20, 3)

    def copy(self):
        return self


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
    # Runtime tests exercise the legacy MJPEG live-frame path directly.
    monkeypatch.setenv("GO2RTC_ENABLED", "false")
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
    cv2.FONT_HERSHEY_SIMPLEX = 0
    cv2.LINE_AA = 16
    cv2.rectangle = lambda image, *args: image
    cv2.putText = lambda image, *args: image
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


def test_heartbeat_reports_real_model_load_state(worker_mod):
    runtime = worker_mod.Runtime()
    runtime._model_loading_name = "custom-forklift"
    runtime._model_error = "Unsupported model graph"
    calls = _record_internal(runtime)

    asyncio.run(runtime._heartbeat())

    assert calls
    payload = calls[-1][1]
    assert payload["model_name"] == "custom-forklift"
    assert payload["model_status"] == "error"
    assert payload["model_error"] == "Unsupported model graph"


def test_prediction_error_is_reported_as_model_runtime_error(worker_mod):
    class BrokenModel:
        def predict(self, *_args, **_kwargs):
            raise RuntimeError("Unsupported ONNX graph")

    runtime = worker_mod.Runtime()
    runtime.model = BrokenModel()
    runtime.model_name = "custom-onnx"
    session = worker_mod.CameraSession(config=worker_mod.CameraConfig.from_api(_camera()))

    assert asyncio.run(runtime._infer(session, _FakeImage())) is None
    assert runtime._model_runtime() == ("custom-onnx", "error", "Unsupported ONNX graph")


def test_ppe_labels_link_no_helmet_to_the_detected_person(worker_mod):
    person = worker_mod.ModelBox((0, 0, 100, 200), "Human", "person", .96, 0)
    bare_head = worker_mod.ModelBox((35, 12, 65, 55), "No-Helmet", "no_helmet", .92, 1)
    helmet = worker_mod.ModelBox((35, 12, 65, 55), "Helmet", "helmet", .92, 2)

    # A direct no-helmet prediction becomes a person-level violation.
    direct = worker_mod.ppe_no_helmet_violations([person, bare_head], {"person", "helmet", "no_helmet"})
    assert direct == [(bare_head, person, False)]
    # A declared no-helmet model must not turn every missed detection into an
    # alarm merely because it saw a person without an explicit helmet box.
    assert worker_mod.ppe_no_helmet_violations([person, helmet], {"person", "helmet", "no_helmet"}) == []
    # A person+helmet-only model can conservatively derive a violation only
    # when no helmet is associated with that person.
    inferred = worker_mod.ppe_no_helmet_violations([person], {"person", "helmet"})
    assert inferred == [(person, person, True)]
    assert worker_mod.ppe_no_helmet_violations([person], {"person"}) == []  # COCO person-only is not PPE


def test_custom_cyrillic_and_russian_class_names_are_normalised(worker_mod):
    """A locally trained model often uses Russian class names; they must not be
    stripped to an empty string by the ASCII-only label normaliser."""
    assert worker_mod.normalise_model_label("Человек") == "person"
    assert worker_mod.normalise_model_label("Рабочий") == "person"
    assert worker_mod.normalise_model_label("Люди") == "person"
    assert worker_mod.normalise_model_label("Без каски") == "no_helmet"
    assert worker_mod.normalise_model_label("Человек без каски") == "no_helmet"
    assert worker_mod.normalise_model_label("Каска") == "helmet"
    assert worker_mod.normalise_model_label("без жилета") == "no_vest"
    assert worker_mod.normalise_model_label("Жилет") == "vest"
    assert worker_mod.normalise_model_label("Person without Hardhat") == "no_helmet"
    assert worker_mod.normalise_model_label("Человек без жилета") == "no_vest"
    semantics = worker_mod.model_semantics({"0": "Человек", "1": "Без каски", "2": "Каска"})
    assert {"person", "no_helmet", "helmet"} <= semantics


def test_ppe_inference_posts_no_helmet_event_for_the_matching_person(worker_mod):
    class Tensor:
        def __init__(self, value): self.value = value
        def cpu(self): return self
        def tolist(self): return self.value

    result = types.SimpleNamespace(
        boxes=types.SimpleNamespace(
            xyxy=Tensor([[0, 0, 100, 200], [35, 12, 65, 55]]),
            cls=Tensor([0, 2]),
            conf=Tensor([.96, .92]),
        )
    )

    class PpeModel:
        def __init__(self): self.names = {0: "Human", 1: "Helmet", 2: "No-Helmet", 3: "Vest"}
        def predict(self, *_args, **_kwargs): return [result]

    runtime = worker_mod.Runtime()
    runtime.model = PpeModel()
    runtime.model_name = "ppe-person-helmet-yolo11"
    posted = []

    async def post(path, data):
        posted.append((path, data))

    runtime.post = post
    session = worker_mod.CameraSession(config=worker_mod.CameraConfig.from_api(_camera()))
    asyncio.run(runtime._infer(session, _FakeImage()))

    assert posted and posted[0][0] == "/api/inference/detections"
    detection = posted[0][1]["detections"][0]
    assert detection["event_type"] == "no_helmet"
    assert detection["bbox"] == [0.0, 0.0, 100.0, 200.0]
    assert detection["person_id"].startswith("cam_01-person-")


def test_camera_test_mode_draws_boxes_without_sending_production_events(worker_mod):
    class Tensor:
        def __init__(self, value): self.value = value
        def cpu(self): return self
        def tolist(self): return self.value

    result = types.SimpleNamespace(boxes=types.SimpleNamespace(
        xyxy=Tensor([[1, 1, 18, 19]]), cls=Tensor([0]), conf=Tensor([.96]),
    ))

    class TestModel:
        def __init__(self): self.names = {0: "no_vest"}
        def predict(self, *_args, **_kwargs): return [result]

    runtime = worker_mod.Runtime()
    runtime.model = TestModel()
    runtime.model_name = "unvalidated-local"
    runtime.model_test_mode = True
    posted = []

    async def post(path, data): posted.append((path, data))
    runtime.post = post
    session = worker_mod.CameraSession(config=worker_mod.CameraConfig.from_api(_camera()))

    visual = asyncio.run(runtime._infer(session, _FakeImage()))

    assert visual is not None and visual.boxes
    assert posted == []


def test_accepted_event_gets_an_annotated_evidence_frame(worker_mod):
    class Tensor:
        def __init__(self, value): self.value = value
        def cpu(self): return self
        def tolist(self): return self.value

    result = types.SimpleNamespace(
        boxes=types.SimpleNamespace(
            xyxy=Tensor([[0, 0, 100, 200], [35, 12, 65, 55]]),
            cls=Tensor([0, 2]),
            conf=Tensor([.96, .92]),
        )
    )

    class PpeModel:
        def __init__(self): self.names = {0: "Human", 1: "Helmet", 2: "No-Helmet", 3: "Vest"}
        def predict(self, *_args, **_kwargs): return [result]

    runtime = worker_mod.Runtime()
    runtime.model = PpeModel()
    runtime.model_name = "ppe-person-helmet-yolo11"
    evidence = []

    async def post(*_args, **_kwargs): return {"accepted": [{"index": 0, "event_id": 42}]}
    async def post_internal_jpeg(path, image): evidence.append((path, image))

    runtime.post = post
    runtime.post_internal_jpeg = post_internal_jpeg
    session = worker_mod.CameraSession(config=worker_mod.CameraConfig.from_api(_camera()))
    asyncio.run(runtime._infer(session, _FakeImage()))

    assert evidence == [("/api/internal/events/42/frame", b"\xff\xd8frame\xff\xd9")]


def test_ppe_boxes_are_drawn_into_the_published_camera_preview(worker_mod):
    class Tensor:
        def __init__(self, value): self.value = value
        def cpu(self): return self
        def tolist(self): return self.value

    result = types.SimpleNamespace(
        boxes=types.SimpleNamespace(
            xyxy=Tensor([[1, 1, 18, 19], [6, 2, 13, 7]]),
            cls=Tensor([0, 1]),
            conf=Tensor([.96, .91]),
        )
    )

    class PpeModel:
        def __init__(self): self.names = {0: "Human", 1: "Helmet", 2: "No-Helmet", 3: "Vest"}
        def predict(self, *_args, **_kwargs): return [result]

    worker_mod.LIVE_PREVIEW_FPS = 20
    rectangles, labels, live = [], [], []
    worker_mod.cv2.rectangle = lambda image, *args: rectangles.append(args) or image
    worker_mod.cv2.putText = lambda image, text, *args: labels.append(text) or image
    _FakeCap.results = [(True, _FakeImage()), (True, _FakeImage())]
    runtime = worker_mod.Runtime()
    runtime.model = PpeModel()
    runtime.model_name = "ppe-person-helmet-yolo11"

    async def post_internal(*_args, **_kwargs): return None
    async def post_internal_jpeg(path, image): live.append((path, image))
    async def post(*_args, **_kwargs): return None

    runtime.post_internal = post_internal
    runtime.post_internal_jpeg = post_internal_jpeg
    runtime.post = post

    async def scenario():
        await runtime.frame(_camera())  # raw high-FPS frame schedules AI
        task = runtime.sessions["cam_01"].inference_task
        assert task is not None
        await task
        await runtime.frame(_camera())  # latest completed boxes paint next frame

    asyncio.run(scenario())

    assert live and live[0][0].endswith("/live-frame")
    assert len(rectangles) >= 2
    assert any(text.startswith("PERSON") for text in labels)
    assert any(text.startswith("HELMET") for text in labels)


def test_custom_model_classes_are_drawn_on_live_preview(worker_mod):
    rectangles, labels = [], []
    worker_mod.cv2.rectangle = lambda image, *args: rectangles.append(args) or image
    worker_mod.cv2.putText = lambda image, text, *args: labels.append(text) or image
    custom = worker_mod.ModelBox((1, 1, 18, 19), "Forklift", "forklift", .91, 0)

    result = worker_mod.draw_detection_overlay(_FakeImage(), [custom], [])

    assert result is not None
    assert rectangles
    assert any(text.startswith("Forklift") for text in labels)


def test_live_preview_does_not_wait_for_slow_ai_inference(worker_mod):
    class Tensor:
        def __init__(self, value): self.value = value
        def cpu(self): return self
        def tolist(self): return self.value

    result = types.SimpleNamespace(boxes=types.SimpleNamespace(xyxy=Tensor([]), cls=Tensor([]), conf=Tensor([])))

    class SlowModel:
        def __init__(self): self.names = {0: "Human"}
        def predict(self, *_args, **_kwargs):
            time.sleep(.16)
            return [result]

    worker_mod.LIVE_PREVIEW_FPS = 60
    _FakeCap.results = [(True, _FakeImage())]
    runtime = worker_mod.Runtime()
    runtime.model = SlowModel()
    runtime.model_name = "slow-model"
    live = []

    async def post_internal(*_args, **_kwargs): return None
    async def post_internal_jpeg(path, image): live.append((path, image))

    runtime.post_internal = post_internal
    runtime.post_internal_jpeg = post_internal_jpeg

    async def scenario():
        started = time.monotonic()
        await runtime.frame(_camera())
        elapsed = time.monotonic() - started
        task = runtime.sessions["cam_01"].inference_task
        assert task is not None and not task.done()
        await task
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < .10, f"preview waited {elapsed:.3f}s for AI"
    assert live and live[0][0].endswith("/live-frame")


def test_ppe_preset_refuses_artifact_without_person_and_helmet_labels(worker_mod, tmp_path, monkeypatch):
    artifact = tmp_path / "ppe.pt"
    artifact.write_bytes(b"weights")

    class WrongPpe:
        def __init__(self, *_args): self.names = {0: "car", 1: "helmet"}

    monkeypatch.setattr(worker_mod, "_yolo_class", lambda: WrongPpe)
    with pytest.raises(RuntimeError, match="неожиданный список классов"):
        worker_mod.load_model_sync({
            "name": "ppe-person-helmet-yolo11", "artifact_uri": f"file://{artifact}",
            "checksum": "", "source": "preset:ppe-person-helmet-yolo11",
        })


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
    for option in ("CAMERA_DECODER", "discardcorrupt", "ignore_err", "nobuffer", "avioflags", "max_delay", "image2pipe", "ZMK_RTSP_URL"):
        assert option in source


def test_compose_forwards_all_camera_runtime_settings():
    import yaml

    compose = yaml.safe_load((WORKER.parents[2] / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"]["inference-worker"]["environment"]
    api_env = compose["services"]["api"]["environment"]
    assert "ZMK_WORKER_TOKEN_FILE" in api_env
    assert "CAMERA_HIGH_FPS_MODE" in api_env
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
        "RTSP_MAX_DELAY_US",
        "OFFLINE_AFTER_FRAMES",
        "RTSP_RECONNECT_SECONDS",
        "CAMERA_CONTROL_POLL_SECONDS",
        "CAMERA_TELEMETRY_SECONDS",
        "CAMERA_SNAPSHOT_SECONDS",
        "CAMERA_LIVE_FPS",
        "CAMERA_INFERENCE_FPS",
        "CAMERA_HIGH_FPS_MODE",
        "CAMERA_HEARTBEAT_SECONDS",
    ):
        assert variable in env


def test_separate_person_and_helmet_models_are_combined(worker_mod):
    class Tensor:
        def __init__(self, value): self.value = value
        def cpu(self): return self
        def tolist(self): return self.value

    person_result = types.SimpleNamespace(boxes=types.SimpleNamespace(
        xyxy=Tensor([[0, 0, 100, 200]]), cls=Tensor([0]), conf=Tensor([.97]),
    ))
    helmet_result = types.SimpleNamespace(boxes=types.SimpleNamespace(
        xyxy=Tensor([]), cls=Tensor([]), conf=Tensor([]),
    ))

    class PersonModel:
        def __init__(self): self.names = {0: "person"}
        def predict(self, *_args, **_kwargs): return [person_result]

    class HelmetModel:
        def __init__(self): self.names = {0: "helmet"}
        def predict(self, *_args, **_kwargs): return [helmet_result]

    runtime = worker_mod.Runtime()
    runtime.model = PersonModel()
    runtime.model_name = "people-model"
    runtime.slot_models["helmet"] = worker_mod.SlotModelRuntime(
        role="helmet", info={"name": "helmet-model"}, model=HelmetModel(), device="cpu",
    )
    posted = []

    async def post(path, data):
        posted.append((path, data))
        return {"accepted": []}

    runtime.post = post
    session = worker_mod.CameraSession(config=worker_mod.CameraConfig.from_api(_camera()))
    visual = asyncio.run(runtime._infer(session, _FakeImage()))

    assert visual is not None and {box.model_name for box in visual.boxes} == {"people-model"}
    assert visual.helmet_violations
    assert posted and posted[0][1]["detections"][0]["model_name"] == "people-model"
    assert posted[0][1]["detections"][0]["event_type"] == "no_helmet"


def test_pipeline_slot_loads_independently_of_primary(worker_mod):
    runtime = worker_mod.Runtime()
    loaded = object()

    async def fake_load(info):
        return str(info["name"]), loaded, "cpu"

    runtime._load_model_async = fake_load

    async def scenario():
        await runtime._refresh_slots([{"role": "helmet", "name": "helmet-model", "artifact_uri": "file:///models/helmet.onnx"}])
        await asyncio.sleep(0)
        await runtime._refresh_slots([{"role": "helmet", "name": "helmet-model", "artifact_uri": "file:///models/helmet.onnx"}])

    asyncio.run(scenario())
    assert runtime.slot_models["helmet"].model is loaded
    assert runtime._ready_models() == [("helmet-model", loaded, "cpu")]


def test_camera_test_uses_lower_test_confidence(worker_mod):
    class Tensor:
        def __init__(self, value): self.value = value
        def cpu(self): return self
        def tolist(self): return self.value

    result = types.SimpleNamespace(boxes=types.SimpleNamespace(
        xyxy=Tensor([[1, 1, 18, 19]]), cls=Tensor([0]), conf=Tensor([.2]),
    ))
    seen = []

    class PersonModel:
        def __init__(self): self.names = {0: "person"}
        def predict(self, *_args, **kwargs):
            seen.append(kwargs["conf"])
            return [result]

    runtime = worker_mod.Runtime()
    runtime.model = PersonModel()
    runtime.model_name = "person-test-model"
    runtime.model_test_mode = True
    runtime.model_test_conf = .10
    session = worker_mod.CameraSession(config=worker_mod.CameraConfig.from_api(_camera()))
    visual = asyncio.run(runtime._infer(session, _FakeImage()))

    assert visual is not None and visual.boxes
    assert seen == [.10]


def test_worker_mirrors_printed_lines_into_the_project_log(worker_mod):
    """Каждая строка stdout worker-а уходит в /api/service-logs (вкладка «Логи»)."""
    runtime = worker_mod.Runtime()
    calls = _record_internal(runtime)
    original = io.StringIO()
    stream = worker_mod._LogShippingStream(original)
    stream.write("inference: camera cam_01 opened via TCP\n")
    stream.write("inference: model load failed: CUDA error\n")
    stream.flush()
    # Зеркалирование не должно ломать обычный вывод в docker logs.
    assert original.getvalue() == "inference: camera cam_01 opened via TCP\ninference: model load failed: CUDA error\n"

    runtime._last_log_ship_at = 0.0
    asyncio.run(runtime._flush_logs())

    assert len(calls) == 1
    path, payload = calls[0]
    assert path == "/api/service-logs" and payload["service"] == "inference"
    levels = {entry["message"]: entry["level"] for entry in payload["entries"]}
    assert levels["inference: camera cam_01 opened via TCP"] == "INFO"
    assert levels["inference: model load failed: CUDA error"] == "ERROR"
    assert all(entry["timestamp"].endswith("+00:00") for entry in payload["entries"])
    assert worker_mod.log_ship_pending() == 0


def test_worker_keeps_buffered_lines_when_the_api_is_unreachable(worker_mod):
    runtime = worker_mod.Runtime()

    async def post_internal(path, data):
        raise httpx.ConnectError("api is down")

    runtime.post_internal = post_internal
    worker_mod.log_ship_capture("camera cam_02 went offline")
    runtime._last_log_ship_at = 0.0
    asyncio.run(runtime._flush_logs())

    assert worker_mod.log_ship_pending() == 1
