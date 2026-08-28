"""Reliable RTSP camera runtime for ZMK Vision.

This worker deliberately separates camera acquisition from ML inference:
RTSP preview, telemetry and reconnects start immediately; Ultralytics is
loaded lazily and asynchronously only when the API has an active model.
A broken GPU/model can therefore never make a configured camera disappear.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import httpx

API = os.getenv("ZMK_API_URL", "http://api:8000").rstrip("/")
API_KEY = os.getenv("ZMK_API_KEY", "")
DEVICE_SETTING = os.getenv("INFERENCE_DEVICE", "auto").strip() or "auto"
# FFmpeg subprocess decoding is the default because it can discard corrupt RTP
# packets and survive a damaged H.264 GOP much better than OpenCV VideoCapture.
CAMERA_DECODER = os.getenv("CAMERA_DECODER", "ffmpeg").strip().lower()
if CAMERA_DECODER not in {"ffmpeg", "opencv"}:
    CAMERA_DECODER = "ffmpeg"


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _buffer_size() -> str:
    raw = os.getenv("RTSP_BUFFER_SIZE", "").strip()
    if not raw:
        return ""
    try:
        return str(max(1, min(1_000_000, int(raw))))
    except ValueError:
        return ""


def _worker_token() -> str:
    token = os.getenv("ZMK_WORKER_TOKEN", "").strip()
    if token:
        return token
    token_file = Path(os.getenv("ZMK_WORKER_TOKEN_FILE", "/models/.worker-token"))
    try:
        return token_file.read_text(encoding="utf-8").strip() if token_file.is_file() else ""
    except OSError:
        return ""


# Kept as an inspectable value for deployment diagnostics. Requests re-read
# the file on every internal call so a token provisioned after startup works.
WORKER_TOKEN = _worker_token()
CONF = _bounded_float("INFERENCE_CONF", 0.5, 0.01, 1.0)
# Test mode deliberately starts lower than production so an uploaded model can
# prove that it sees people before its validation metrics are known.
TEST_CONF = _bounded_float("MODEL_TEST_CONF", 0.10, 0.01, 0.95)
RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "auto").strip().lower()
if RTSP_TRANSPORT not in {"auto", "tcp", "udp"}:
    RTSP_TRANSPORT = "auto"
TRANSPORT_ORDER = ["tcp", "udp"] if RTSP_TRANSPORT == "auto" else [RTSP_TRANSPORT]
# Modern FFmpeg RTSP builds use `timeout` (microseconds). Some legacy builds
# retain `stimeout`, so the operator can explicitly switch it without a code
# change when using a custom OpenCV/FFmpeg image.
RTSP_TIMEOUT_OPTION = os.getenv("RTSP_TIMEOUT_OPTION", "timeout").strip().lower()
if RTSP_TIMEOUT_OPTION not in {"timeout", "stimeout"}:
    RTSP_TIMEOUT_OPTION = "timeout"
_RTSP_BUFSIZE = _buffer_size()
_RTSP_STIMEOUT = _bounded_int("RTSP_STIMEOUT", 5_000_000, 100_000, 120_000_000)
OPEN_TIMEOUT_MS = _bounded_int("RTSP_OPEN_TIMEOUT_MS", 8_000, 1_000, 120_000)
READ_TIMEOUT_MS = _bounded_int("RTSP_READ_TIMEOUT_MS", 8_000, 1_000, 120_000)
OFFLINE_AFTER = _bounded_int("OFFLINE_AFTER_FRAMES", 3, 1, 100)
RECONNECT_MIN = _bounded_int("RTSP_RECONNECT_SECONDS", 5, 0, 3_600)
CONTROL_POLL_SECONDS = _bounded_float("CAMERA_CONTROL_POLL_SECONDS", 2.0, 0.2, 60.0)
TELEMETRY_INTERVAL_SECONDS = _bounded_float("CAMERA_TELEMETRY_SECONDS", 5.0, 1.0, 60.0)
SNAPSHOT_INTERVAL_SECONDS = _bounded_float("CAMERA_SNAPSHOT_SECONDS", 3.0, 0.5, 60.0)
HEARTBEAT_INTERVAL_SECONDS = _bounded_float("CAMERA_HEARTBEAT_SECONDS", 5.0, 1.0, 60.0)
# RTSP servers often begin a newly attached client in the middle of a GOP.
# Give H.264 decoding time to reach the next IDR/keyframe before reconnecting.
KEYFRAME_GRACE_SECONDS = _bounded_float("RTSP_KEYFRAME_GRACE_SECONDS", 15.0, 1.0, 120.0)
KEYFRAME_RETRY_SECONDS = 0.2
# Small RTP jitter buffer for a near-live stream without reordering seconds of video.
RTSP_MAX_DELAY_US = _bounded_int("RTSP_MAX_DELAY_US", 100_000, 0, 2_000_000)
FFMPEG_FRAME_MAX_BYTES = _bounded_int("FFMPEG_FRAME_MAX_BYTES", 5_000_000, 100_000, 20_000_000)
# Browser MJPEG preview follows the source as closely as the host permits.
# The actual rate is still bounded by the camera/NVR and never fabricated.
LIVE_PREVIEW_FPS = _bounded_float("CAMERA_LIVE_FPS", 60.0, 0.0, 60.0)
# ML is intentionally sampled separately from the preview. A slow CPU model
# must not drag a 25/30/50 FPS camera wall down to inference speed.
INFERENCE_FPS = _bounded_float("CAMERA_INFERENCE_FPS", 8.0, 0.1, 30.0)
HIGH_FPS_MODE = os.getenv("CAMERA_HIGH_FPS_MODE", "true").strip().lower() not in {"0", "false", "no", "off"}
if HIGH_FPS_MODE and LIVE_PREVIEW_FPS <= 20:
    # Preserve a deliberate low setting by disabling CAMERA_HIGH_FPS_MODE;
    # otherwise transparently upgrade the former 20 FPS project default.
    LIVE_PREVIEW_FPS = 60.0
EVENT_CLASSES = {
    "no_helmet",
    "no_vest",
    "phone_usage",
    "smoking",
    "restricted_zone",
    "immobility",
}
_URL_SECRET = re.compile(r"rtsps?://[^\s'\"<>]+", re.IGNORECASE)
_YOLO_CLASS: Any | None = None

# A PPE model may call the same concepts differently ("Human", "hat",
# "No-Helmet", ...).  Normalising them here makes a genuine person+helmet
# model usable without forcing its author to name classes exactly like our
# event API.  COCO `person` alone is never enough to raise a violation.
_PERSON_LABELS = {"person", "human", "worker", "people"}
_HELMET_LABELS = {"helmet", "hardhat", "hard_hat", "hat", "safety_helmet"}
_NO_HELMET_LABELS = {
    "no_helmet", "nohelmet", "no_hat", "nohat", "no_hardhat", "nohardhat",
    "without_helmet", "withouthelmet", "person_without_helmet", "person_no_helmet",
}
_VEST_LABELS = {"vest", "safety_vest", "hi_vis_vest", "hi_vis", "reflective_vest", "workwear", "coveralls", "robe"}
_NO_VEST_LABELS = {"no_vest", "novest", "without_vest", "withoutvest", "no_workwear", "noworkwear", "no_coveralls", "nocoveralls"}


@dataclass(frozen=True)
class ModelBox:
    """One normalised YOLO detection used to build safety events."""

    bbox: tuple[float, float, float, float]
    label: str
    semantic: str
    confidence: float
    index: int
    # The pipeline can combine separate person, helmet and workwear models.
    # Keep provenance so the event gateway validates the correct active slot.
    model_name: str = ""


@dataclass(frozen=True)
class InferenceVisual:
    """Latest real detector output, reused only on matching-size live frames."""

    shape: tuple[int, int]
    boxes: list[ModelBox]
    helmet_violations: list[tuple[ModelBox, ModelBox | None, bool]]
    vest_violations: list[tuple[ModelBox, ModelBox | None, bool]] = field(default_factory=list)


def normalise_model_label(value: object) -> str:
    raw = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    if raw in _PERSON_LABELS:
        return "person"
    if raw in _HELMET_LABELS:
        return "helmet"
    if raw in _NO_HELMET_LABELS:
        return "no_helmet"
    if raw in _VEST_LABELS:
        return "vest"
    if raw in _NO_VEST_LABELS:
        return "no_vest"
    return raw


def model_semantics(names: object) -> set[str]:
    """Return semantic classes declared by a YOLO model's names mapping."""

    values = names.values() if isinstance(names, dict) else names if isinstance(names, (list, tuple)) else ()
    return {normalise_model_label(value) for value in values}


def _box_center(box: ModelBox) -> tuple[float, float]:
    x1, y1, x2, y2 = box.bbox
    return (x1 + x2) / 2, (y1 + y2) / 2


def _person_for_box(box: ModelBox, people: list[ModelBox]) -> ModelBox | None:
    """Associate a head/helmet/body box to the most plausible person box.

    PPE datasets do not all label the missing helmet equally: some mark the
    head and some mark the whole worker.  Center-in-expanded-person plus a
    containment score supports both variants while preventing an unrelated
    hardhat from being assigned to a neighbouring worker.
    """

    cx, cy = _box_center(box)
    best: tuple[float, ModelBox] | None = None
    for person in people:
        x1, y1, x2, y2 = person.bbox
        width, height = max(1.0, x2 - x1), max(1.0, y2 - y1)
        if not (x1 - .15 * width <= cx <= x2 + .15 * width and y1 - .25 * height <= cy <= y2 + .15 * height):
            continue
        # Prefer a person that actually contains the child box's centre, then
        # a closer/more confident anchor when workers overlap in the frame.
        centrality = 1 - min(1.0, abs(cx - (x1 + x2) / 2) / width + abs(cy - (y1 + y2) / 2) / height)
        score = person.confidence + centrality * .01
        if best is None or score > best[0]:
            best = (score, person)
    return best[1] if best else None


def ppe_no_helmet_violations(boxes: list[ModelBox], declared: set[str]) -> list[tuple[ModelBox, ModelBox | None, bool]]:
    """Return (evidence, person, inferred) no-helmet violations.

    A model with an explicit `no_helmet` label is trusted to make that
    classification.  For a model with *only* person and helmet classes, use a
    conservative relation: a detected person that has no matching helmet is a
    violation.  A generic COCO model cannot enter either path because it has no
    helmet class, so it never turns every person into a false alarm.
    """

    people = [box for box in boxes if box.semantic == "person"]
    missing = [box for box in boxes if box.semantic == "no_helmet"]
    if missing:
        result: list[tuple[ModelBox, ModelBox | None, bool]] = []
        for evidence in missing:
            person = _person_for_box(evidence, people) if people else None
            # A PPE model that declares people must link a bare-head detection
            # to one; otherwise it may belong to a poster/background object.
            if people and person is None:
                continue
            result.append((evidence, person, False))
        return result

    # Models trained to output `no_helmet` already made the classification. Do
    # not second-guess an absent detection with a noisier absence heuristic.
    if "no_helmet" in declared or not people or "helmet" not in declared:
        return []
    helmets = [box for box in boxes if box.semantic == "helmet"]
    protected = {person.index for helmet in helmets if (person := _person_for_box(helmet, people)) is not None}
    return [(person, person, True) for person in people if person.index not in protected]


def ppe_no_vest_violations(boxes: list[ModelBox], declared: set[str]) -> list[tuple[ModelBox, ModelBox | None, bool]]:
    """Same conservative cross-model relation as helmet detection for workwear."""
    people = [box for box in boxes if box.semantic == "person"]
    missing = [box for box in boxes if box.semantic == "no_vest"]
    if missing:
        result=[]
        for evidence in missing:
            person = _person_for_box(evidence, people) if people else None
            if people and person is None:
                continue
            result.append((evidence, person, False))
        return result
    # Unlike helmets, a vest can be hidden behind tools, an arm or a partial
    # frame. Do not turn a missed vest box into a production alarm. A separate
    # workwear model must expose an explicit `no_vest` class to raise this event.
    return []


def _label_at(names: object, class_id: int) -> str:
    if isinstance(names, dict):
        return str(names.get(class_id, names.get(str(class_id), class_id)))
    if isinstance(names, (list, tuple)) and 0 <= class_id < len(names):
        return str(names[class_id])
    return str(class_id)


def _person_id(camera_id: str, box: ModelBox) -> str:
    x, y = _box_center(box)
    return f"{camera_id}-person-{int(x // 100)}-{int(y // 100)}"


def draw_detection_overlay(
    image: Any,
    boxes: list[ModelBox],
    helmet_violations: list[tuple[ModelBox, ModelBox | None, bool]],
    vest_violations: list[tuple[ModelBox, ModelBox | None, bool]] | None = None,
) -> Any | None:
    """Draw real detector boxes onto a live frame for the browser preview.

    The stream is not decorated with invented boxes: every rectangle comes
    directly from the current YOLO result.  A person linked to a real
    no-helmet violation is red; a regular person is blue; a detected helmet is
    green.  ASCII labels are intentional because OpenCV's built-in Hershey
    font cannot render Cyrillic reliably in a minimal container.
    """

    # A custom local model may use classes outside ZMK's safety-event schema.
    # They must still be visible on the camera preview; only event generation
    # below remains limited to recognised safety semantics.
    if not boxes:
        return None
    try:
        canvas = image.copy()
        height, width = canvas.shape[:2]
        thickness = max(1, round(min(height, width) / 500))
        scale = max(.35, min(.7, min(height, width) / 1000))
        vest_violations = vest_violations or []
        helmet_people = {person.index for _, person, _ in helmet_violations if person is not None}
        vest_people = {person.index for _, person, _ in vest_violations if person is not None}
        for box in boxes:
            x1, y1, x2, y2 = box.bbox
            left = max(0, min(width - 1, round(x1)))
            top = max(0, min(height - 1, round(y1)))
            right = max(0, min(width - 1, round(x2)))
            bottom = max(0, min(height - 1, round(y2)))
            if right <= left or bottom <= top:
                continue
            if box.semantic == "person":
                no_helmet = box.index in helmet_people
                no_vest = box.index in vest_people
                colour = (50, 55, 235) if no_helmet or no_vest else (235, 170, 30)  # BGR: red / blue
                title = "NO HELMET & VEST" if no_helmet and no_vest else "NO HELMET" if no_helmet else "NO VEST" if no_vest else "PERSON"
            elif box.semantic == "helmet":
                colour, title = (55, 205, 75), "HELMET"
            elif box.semantic == "no_helmet":
                colour, title = (50, 55, 235), "NO HELMET"
            elif box.semantic == "vest":
                colour, title = (200, 80, 170), "VEST"
            elif box.semantic == "no_vest":
                colour, title = (50, 55, 235), "NO VEST"
            else:
                # OpenCV Hershey fonts cannot safely render arbitrary Unicode,
                # but a clean ASCII fallback still proves that the custom model
                # is producing real boxes on the live camera.
                title=re.sub(r"[^A-Za-z0-9_. -]+","?",str(box.label or box.semantic)).strip()[:32] or f"CLASS {box.index}"
                colour = (0, 165, 255)
            cv2.rectangle(canvas, (left, top), (right, bottom), colour, thickness)
            cv2.putText(
                canvas,
                f"{title} {box.confidence:.0%}",
                (left, max(14, top - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                scale,
                colour,
                thickness,
                cv2.LINE_AA,
            )
        return canvas
    except Exception:  # noqa: BLE001 - drawing is optional and must never stop RTSP.
        # Annotation must never make camera transport fail. Returning None
        # preserves the original decoded frame as the preview fallback.
        return None


def redact_error(value: object, limit: int = 300) -> str:
    """Return an operator-safe error: never echo an RTSP credential."""
    text = _URL_SECRET.sub("<rtsp-url>", str(value or "")).replace("\n", " ").strip()
    return text[:limit] or "Неизвестная ошибка потока"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_device() -> str:
    """Resolve CUDA only while loading an actual model, never at startup."""
    if DEVICE_SETTING != "auto":
        return DEVICE_SETTING
    try:
        import torch

        return "0" if torch.cuda.is_available() else "cpu"
    except (ImportError, OSError, RuntimeError):
        return "cpu"


def _yolo_class():
    global _YOLO_CLASS
    if _YOLO_CLASS is None:
        from ultralytics import YOLO

        _YOLO_CLASS = YOLO
    return _YOLO_CLASS


def load_model_sync(info: dict[str, Any]) -> tuple[str, Any, str]:
    """Blocking model work executed in a background thread."""
    artifact = str(info["artifact_uri"]).removeprefix("file://")
    path = Path(artifact)
    if not path.is_file():
        raise RuntimeError("Артефакт активной модели не найден")
    checksum = str(info.get("checksum") or "")
    if checksum and file_sha256(path).lower() != checksum.lower():
        raise RuntimeError("Контрольная сумма активной модели не совпадает")
    device = resolve_device()
    model = _yolo_class()(str(path))
    # The downloadable PPE baseline must still expose the semantic classes it
    # promises after deserialisation; otherwise never activate a wrong/corrupt
    # artifact and silently generate unrelated events.
    if str(info.get("source") or "") == "preset:ppe-person-helmet-yolo11":
        semantics = model_semantics(getattr(model, "names", {}))
        if "person" not in semantics or not ({"helmet", "no_helmet"} & semantics):
            raise RuntimeError("PPE-пресет имеет неожиданный список классов")
    return str(info["name"]), model, device


@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    name: str
    rtsp_url: str
    fps_limit: float
    restart_token: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> CameraConfig:
        return cls(
            camera_id=str(raw["id"]),
            name=str(raw.get("name") or raw["id"]),
            rtsp_url=str(raw["rtsp_url"]),
            fps_limit=max(0.1, min(60.0, float(raw.get("fps_limit") or 30))),
            restart_token=str(raw.get("restart_requested_at") or ""),
        )

    @property
    def signature(self) -> tuple[str, float, str]:
        return self.rtsp_url, self.fps_limit, self.restart_token


@dataclass
class CameraSession:
    config: CameraConfig
    capture: Any | None = None
    status: str = "connecting"
    transport_index: int = 0
    failures: int = 0
    next_attempt_at: float = 0.0
    next_frame_at: float = 0.0
    last_telemetry_at: float = 0.0
    telemetry_window_started: float = 0.0
    frames_in_window: int = 0
    last_snapshot_at: float = 0.0
    last_live_at: float = 0.0
    last_error: str = ""
    opened_at: float = 0.0
    received_first_frame: bool = False
    frame_task: asyncio.Task[None] | None = None
    # The preview decoder stays independent from the ML task. This keeps live
    # MJPEG fluid even when a CPU/GPU inference pass takes longer than a frame.
    inference_task: asyncio.Task[InferenceVisual | None] | None = None
    next_inference_at: float = 0.0
    latest_visual: InferenceVisual | None = None
    restart_pending: bool = False
    ffmpeg: asyncio.subprocess.Process | None = None
    ffmpeg_buffer: bytearray = field(default_factory=bytearray)
    ffmpeg_stderr_task: asyncio.Task[None] | None = None
    decoder_error: str = ""

    @property
    def transport(self) -> str:
        return TRANSPORT_ORDER[self.transport_index % len(TRANSPORT_ORDER)]


@dataclass
class SlotModelRuntime:
    role: str
    info: dict[str, Any]
    model: Any | None = None
    device: str = DEVICE_SETTING
    task: asyncio.Task[tuple[str, Any, str]] | None = None
    error: str = ""
    retry_at: float = 0.0


class Runtime:
    """Owns camera sessions, API protocol and optional model inference."""

    def __init__(self) -> None:
        self.sessions: dict[str, CameraSession] = {}
        self._deferred_releases: list[CameraSession] = []
        self._transport_cursor: dict[str, int] = {}
        self._http: httpx.AsyncClient | None = None
        self._next_control_poll = 0.0
        self._last_heartbeat_at = 0.0
        self._last_no_camera_log = 0.0
        self._last_loop_error_at = 0.0
        self._capture_open_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()
        self.model: Any | None = None
        self.model_name = ""
        # Explicit camera tests draw boxes but never deliver detections/events.
        self.model_test_mode = False
        self.model_test_conf = TEST_CONF
        self.device = DEVICE_SETTING
        self._model_task: asyncio.Task[tuple[str, Any, str]] | None = None
        self._model_loading_name = ""
        self._model_error = ""
        self._model_retry_at = 0.0
        self._no_model_announced = False
        self.slot_models: dict[str, SlotModelRuntime] = {}

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        internal: bool = False,
    ) -> Any:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        headers: dict[str, str] = {}
        if internal:
            headers["X-Worker-Token"] = _worker_token()
        elif API_KEY:
            headers["X-API-Key"] = API_KEY
        response = await self._http.request(method, API + path, json=payload, headers=headers)
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()

    async def get(self, path: str, internal: bool = False) -> Any:
        return await self._request("GET", path, internal=internal)

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        # Detection ingestion is worker-only even though it lives outside the
        # /api/internal namespace; browser password sessions must not block it.
        return await self._request("POST", path, data, internal=path.startswith("/api/inference/"))

    async def post_internal(self, path: str, data: dict[str, Any]) -> Any:
        return await self._request("POST", path, data, internal=True)

    async def post_internal_jpeg(self, path: str, image: bytes) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        response = await self._http.post(
            API + path,
            content=image,
            headers={"X-Worker-Token": _worker_token(), "Content-Type": "image/jpeg"},
        )
        response.raise_for_status()

    def _next_transport(self, camera_id: str) -> str:
        """Compatibility helper: initial auto attempt is TCP, then UDP."""
        index = self._transport_cursor.get(camera_id, 0)
        transport = TRANSPORT_ORDER[index % len(TRANSPORT_ORDER)]
        self._transport_cursor[camera_id] = (index + 1) % len(TRANSPORT_ORDER)
        return transport

    def _open_capture(self, url: str, transport: str):
        """Open RTSP with timeouts supplied *before* FFmpeg connects.

        Calling CAP_PROP_OPEN_TIMEOUT_MSEC after VideoCapture(url, ...) is too
        late: OpenCV has already entered its 30-second default connection path.
        The empty-capture + open(params=...) form is supported by modern OpenCV
        and is the reason unreachable streams now fail in ~OPEN_TIMEOUT_MS.
        """
        options = [
            f"rtsp_transport;{transport}",
            f"{RTSP_TIMEOUT_OPTION};{_RTSP_STIMEOUT}",
        ]
        if _RTSP_BUFSIZE:
            options.append(f"buffer_size;{_RTSP_BUFSIZE}")
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(options)

        backend = getattr(cv2, "CAP_FFMPEG", 0)
        params: list[int] = []
        for name, value in (
            ("CAP_PROP_OPEN_TIMEOUT_MSEC", OPEN_TIMEOUT_MS),
            ("CAP_PROP_READ_TIMEOUT_MSEC", READ_TIMEOUT_MS),
        ):
            prop = getattr(cv2, name, None)
            if prop is not None:
                params.extend([prop, value])

        capture = cv2.VideoCapture()
        try:
            # Python OpenCV >= 4.5 accepts (filename, apiPreference, params).
            capture.open(url, backend, params)
        except (TypeError, AttributeError):
            # Compatibility fallback for bindings that do not expose params.
            # Modern builds must not reach this path: constructing with a URL
            # first would reintroduce the built-in 30-second timeout.
            self._release(capture)
            capture = cv2.VideoCapture(url, backend)
        except Exception:  # noqa: BLE001 - fail fast instead of falling back to 30 seconds.
            self._release(capture)

        # These are still useful for read buffering; unlike the open params,
        # they are not relied upon to control connection timeout.
        for name, value in (("CAP_PROP_BUFFERSIZE", 1),):
            prop = getattr(cv2, name, None)
            if prop is not None:
                try:
                    capture.set(prop, value)
                except (AttributeError, RuntimeError, OSError, ValueError):
                    pass
        return capture

    @staticmethod
    def _is_open(capture: Any | None) -> bool:
        try:
            return bool(capture and capture.isOpened())
        except (AttributeError, RuntimeError, OSError, ValueError):
            return False

    @staticmethod
    def _release(capture: Any | None) -> None:
        if capture is None:
            return
        try:
            capture.release()
        except (AttributeError, RuntimeError, OSError, ValueError):
            pass

    @staticmethod
    def _stop_ffmpeg(session: CameraSession) -> None:
        process = session.ffmpeg
        session.ffmpeg = None
        session.ffmpeg_buffer.clear()
        if session.ffmpeg_stderr_task is not None and not session.ffmpeg_stderr_task.done():
            session.ffmpeg_stderr_task.cancel()
        session.ffmpeg_stderr_task = None
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    def _release_session(self, session: CameraSession) -> None:
        self._release(session.capture)
        session.capture = None
        self._stop_ffmpeg(session)
        if session.inference_task is not None and not session.inference_task.done():
            session.inference_task.cancel()
        session.inference_task = None
        session.latest_visual = None

    @staticmethod
    def _frame_task_finished(session: CameraSession) -> bool:
        return session.frame_task is None or session.frame_task.done()

    @staticmethod
    def _inference_task_finished(session: CameraSession) -> bool:
        return session.inference_task is None or session.inference_task.done()

    def _release_when_safe(self, session: CameraSession) -> bool:
        """Never release a VideoCapture while a native read is in progress."""
        if session.inference_task is not None and not session.inference_task.done():
            session.inference_task.cancel()
        if not self._frame_task_finished(session):
            if session not in self._deferred_releases:
                self._deferred_releases.append(session)
            return False
        self._release_session(session)
        return True

    def _cleanup_deferred_releases(self) -> None:
        for session in list(self._deferred_releases):
            if self._frame_task_finished(session):
                self._release_session(session)
                self._deferred_releases.remove(session)

    def _log(self, message: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_loop_error_at >= 10:
            print(f"inference: {message}", flush=True)
            self._last_loop_error_at = now

    async def _report(
        self,
        session: CameraSession,
        status: str,
        latency_ms: int = 0,
        error: str = "",
        *,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        status_changed = session.status != status or session.last_error != error
        if (
            not force
            and not status_changed
            and session.last_telemetry_at
            and now - session.last_telemetry_at < TELEMETRY_INTERVAL_SECONDS
        ):
            return

        if not session.telemetry_window_started:
            session.telemetry_window_started = now
        elapsed = max(0.001, now - session.telemetry_window_started)
        # Do not publish a meaningless spike from the very first frame; the
        # next telemetry window contains a real measured rate.
        fps = round(session.frames_in_window / elapsed, 2) if session.frames_in_window and elapsed >= 1 else 0.0
        try:
            await self.post_internal(
                f"/api/internal/cameras/{session.config.camera_id}/telemetry",
                {"status": status, "fps": fps, "latency_ms": max(0, latency_ms), "error": error},
            )
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            self._log(f"telemetry for {session.config.camera_id} rejected: {redact_error(exc)}")
        finally:
            session.status = status
            session.last_error = error
            session.last_telemetry_at = now
            session.telemetry_window_started = now
            session.frames_in_window = 0

    def _model_runtime(self) -> tuple[str, str, str]:
        """Return (name, state, safe_error) for API/UI model lifecycle status."""
        if self._model_error:
            return self._model_loading_name or self.model_name, "error", self._model_error
        if self.model is not None and self.model_name:
            return self.model_name, "ready", ""
        if self._model_task is not None and not self._model_task.done():
            return self._model_loading_name, "loading", ""
        return "", "none", ""

    async def _heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat_at < HEARTBEAT_INTERVAL_SECONDS:
            return
        status = "running" if self.sessions else "idle"
        model_name, model_status, model_error = self._model_runtime()
        slot_detail=",".join(f"{state.role}:{state.info.get('name')}:{'ready' if state.model is not None else 'loading' if state.task is not None else 'error' if state.error else 'waiting'}" for state in self.slot_models.values()) or "none"
        detail = f"cameras={len(self.sessions)} model={model_name or 'none'} slots={slot_detail} state={model_status} test={str(self.model_test_mode).lower()} test_conf={self.model_test_conf:.2f}"
        if model_error:
            detail += f" error={model_error}"
        try:
            await self.post_internal(
                "/api/internal/inference/heartbeat",
                {"status": status, "detail": detail[:300], "camera_count": len(self.sessions), "model_name": model_name, "model_status": model_status, "model_error": model_error[:300]},
            )
            self._last_heartbeat_at = now
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            self._log(f"heartbeat rejected: {redact_error(exc)}")

    def _sync_cameras(self, raw_cameras: list[dict[str, Any]]) -> None:
        desired = {config.camera_id: config for config in map(CameraConfig.from_api, raw_cameras)}
        for camera_id in set(self.sessions) - set(desired):
            session = self.sessions.pop(camera_id)
            self._release_when_safe(session)
            self._transport_cursor.pop(camera_id, None)

        for camera_id, config in desired.items():
            session = self.sessions.get(camera_id)
            if session is None:
                self.sessions[camera_id] = CameraSession(config=config)
                continue
            if session.config.signature != config.signature:
                session.config = config
                session.status = "connecting"
                session.transport_index = 0
                session.failures = 0
                session.next_attempt_at = 0
                session.next_frame_at = 0
                session.last_error = ""
                session.last_live_at = 0
                session.opened_at = 0
                session.received_first_frame = False
                session.next_inference_at = 0
                session.latest_visual = None
                if session.inference_task is not None and not session.inference_task.done():
                    session.inference_task.cancel()
                session.inference_task = None
                if self._frame_task_finished(session):
                    self._release_session(session)
                else:
                    # Let the current native read return naturally, then reopen
                    # with the new RTSP URL/restart token.
                    session.restart_pending = True
            else:
                session.config = config

    def _ready_models(self) -> list[tuple[str, Any, str]]:
        ready=[]
        if self.model is not None and self.model_name:
            ready.append((self.model_name,self.model,self.device))
        seen={self.model_name} if self.model_name else set()
        for slot in self.slot_models.values():
            name=str(slot.info.get("name") or "")
            if slot.model is not None and name and name not in seen:
                ready.append((name,slot.model,slot.device)); seen.add(name)
        return ready

    def _clear_inference_visuals(self) -> None:
        for session in self.sessions.values():
            if session.inference_task is not None and not session.inference_task.done():
                session.inference_task.cancel()
            session.inference_task = None
            session.latest_visual = None
            session.next_inference_at = 0

    async def _refresh_slots(self, infos: list[dict[str, Any]]) -> None:
        desired={str(info.get("role")):dict(info) for info in infos if str(info.get("role")) and str(info.get("name"))}
        changed=False
        for role in set(self.slot_models)-set(desired):
            state=self.slot_models.pop(role)
            if state.task is not None and not state.task.done():
                state.task.cancel()
            changed=True
        now=time.monotonic()
        for role,info in desired.items():
            state=self.slot_models.get(role)
            if state is None or state.info.get("name")!=info.get("name"):
                if state is not None and state.task is not None and not state.task.done():
                    state.task.cancel()
                state=SlotModelRuntime(role=role,info=info)
                self.slot_models[role]=state
                changed=True
            if state.task is not None and state.task.done():
                task=state.task; state.task=None
                try:
                    loaded_name,model,device=task.result()
                    if loaded_name==str(state.info.get("name")):
                        state.model=model; state.device=device; state.error=""
                        self._log(f"pipeline model loaded: {role}={loaded_name} (device={device})",force=True)
                except Exception as exc:  # noqa: BLE001 - model backends vary.
                    state.model=None; state.retry_at=now+15; state.error=redact_error(exc)
                    self._log(f"pipeline model unavailable: {role}={state.info.get('name')} error={state.error}",force=True)
            if state.model is None and state.task is None and now>=state.retry_at:
                state.error=""
                state.task=asyncio.create_task(self._load_model_async(dict(state.info)),name=f"slot-model-load-{role}-{state.info.get('name')}")
                self._log(f"loading pipeline model: {role}={state.info.get('name')}",force=True)
        if changed:
            self._clear_inference_visuals()

    async def _load_model_async(self, info: dict[str, Any]) -> tuple[str, Any, str]:
        return await asyncio.to_thread(load_model_sync, info)

    async def _refresh_model(self, info: dict[str, Any] | None) -> None:
        now = time.monotonic()
        if not info:
            if self._model_task is not None and not self._model_task.done():
                self._model_task.cancel()
            self._model_task = None
            self.model = None
            self.model_name = ""
            self.model_test_mode = False
            self.model_test_conf = TEST_CONF
            self._model_loading_name = ""
            self._model_error = ""
            if not self._no_model_announced:
                self._log("no active model; camera preview and telemetry remain enabled", force=True)
                self._no_model_announced = True
            return

        wanted_name = str(info["name"])
        requested_test_mode = bool(info.get("test_mode"))
        try:
            requested_test_conf=max(.01,min(.95,float(info.get("test_conf") if info.get("test_conf") is not None else TEST_CONF)))
        except (TypeError,ValueError):
            requested_test_conf=TEST_CONF
        self._no_model_announced = False
        if self.model_name == wanted_name and self.model is not None:
            self.model_test_mode = requested_test_mode
            self.model_test_conf = requested_test_conf
            self._model_loading_name = wanted_name
            self._model_error = ""
            return

        if self._model_task is not None:
            if not self._model_task.done():
                self.model_test_mode = requested_test_mode
                self.model_test_conf = requested_test_conf
                return
            task = self._model_task
            self._model_task = None
            try:
                loaded_name, model, device = task.result()
            except Exception as exc:  # noqa: BLE001 - model backends raise heterogeneous load errors.
                self._model_retry_at = now + 15
                self._model_loading_name = wanted_name
                self._model_error = redact_error(exc)
                self._log(f"model unavailable; camera capture continues: {self._model_error}", force=True)
                return
            if loaded_name == wanted_name:
                self.model = model
                self.model_name = loaded_name
                self._model_loading_name = loaded_name
                self._model_error = ""
                self.device = device
                self._log(f"active model loaded: {loaded_name} (device={device})", force=True)
                return

        if now < self._model_retry_at:
            return
        self.model = None
        self.model_name = ""
        for session in self.sessions.values():
            if session.inference_task is not None and not session.inference_task.done():
                session.inference_task.cancel()
            session.inference_task = None
            session.latest_visual = None
            session.next_inference_at = 0
        self._model_loading_name = wanted_name
        self.model_test_mode = requested_test_mode
        self.model_test_conf = requested_test_conf
        self._model_error = ""
        self._model_task = asyncio.create_task(self._load_model_async(dict(info)), name=f"model-load-{wanted_name}")
        self._log(f"loading active model {wanted_name} in background", force=True)

    async def load_model(self) -> None:
        """Compatibility entry point used by unit tests and the control loop."""
        info = await self.get("/api/internal/active-model", internal=True)
        await self._refresh_model(info)

    async def _refresh_control(self) -> None:
        cameras = await self.get("/api/internal/cameras", internal=True)
        self._sync_cameras(cameras)
        try:
            pipeline = await self.get("/api/internal/active-models", internal=True)
            info=pipeline.get("primary") if isinstance(pipeline,dict) else None
            slots=pipeline.get("slots",[]) if isinstance(pipeline,dict) and isinstance(pipeline.get("slots"),list) else []
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            self._log(f"pipeline query failed; trying legacy active model: {redact_error(exc)}")
            try:
                info=await self.get("/api/internal/active-model", internal=True)
            except (httpx.HTTPError, OSError, RuntimeError, ValueError):
                info=None
            slots=[]
        await self._refresh_model(info)
        await self._refresh_slots(slots)
        if not self._ready_models() and not any(state.task is not None for state in self.slot_models.values()):
            self._clear_inference_visuals()

        if not self.sessions and time.monotonic() - self._last_no_camera_log >= 30:
            self._log("no enabled RTSP cameras returned by API; set RTSP_CAM_01 or add a camera", force=True)
            self._last_no_camera_log = time.monotonic()

    async def _open(self, session: CameraSession) -> bool:
        now = time.monotonic()
        if self._is_open(session.capture):
            return True
        if now < session.next_attempt_at:
            return False

        await self._report(session, "connecting", error="", force=session.status != "connecting")
        transport = session.transport
        started = time.perf_counter()
        try:
            # FFmpeg options are process-global in OpenCV, so serialise only
            # the tiny open section; reads for other cameras remain concurrent.
            async with self._capture_open_lock:
                # Do not wrap native OpenCV open() in wait_for(). Cancelling
                # the asyncio wrapper while FFmpeg is still inside C++ can
                # leave a live native object behind and later crash Python
                # (exit 139). The params passed to open() enforce the timeout.
                capture = await asyncio.to_thread(self._open_capture, session.config.rtsp_url, transport)
        except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
            await self._failed_open(session, redact_error(exc), round((time.perf_counter() - started) * 1000))
            return False

        if not self._is_open(capture):
            self._release(capture)
            await self._failed_open(session, f"не удалось открыть RTSP по {transport.upper()}", round((time.perf_counter() - started) * 1000))
            return False

        session.capture = capture
        session.failures = 0
        session.next_attempt_at = 0
        session.opened_at = time.monotonic()
        session.received_first_frame = False
        self._log(f"camera {session.config.camera_id} opened via {transport.upper()}", force=True)
        return True

    async def _drain_ffmpeg_stderr(self, session: CameraSession, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                text = redact_error(line.decode(errors="replace"))
                if text:
                    # Keep only the most recent decoder hint. Printing every
                    # damaged H.264 macroblock would flood operator logs.
                    session.decoder_error = text
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError):
            return

    async def _start_ffmpeg(self, session: CameraSession) -> None:
        if session.ffmpeg is not None and session.ffmpeg.returncode is None:
            return
        self._stop_ffmpeg(session)
        await self._report(session, "connecting", error="", force=session.status != "connecting")
        env = os.environ.copy()
        env.update(
            {
                "ZMK_RTSP_URL": session.config.rtsp_url,
                "ZMK_RTSP_TRANSPORT": session.transport,
                "ZMK_RTSP_MAX_DELAY_US": str(RTSP_MAX_DELAY_US),
            }
        )
        # URL is intentionally read from an environment variable inside the
        # container. It never appears in a process command line or log.
        command = (
            'exec ffmpeg -hide_banner -nostdin -loglevel warning '
            '-rtsp_transport "$ZMK_RTSP_TRANSPORT" '
            # Worker-owned asyncio timeout safely terminates this subprocess;
            # avoid version-specific FFmpeg timeout flags that make some
            # packaged builds exit immediately with status 255.
            '-fflags +nobuffer+genpts+discardcorrupt -avioflags direct '
            '-err_detect ignore_err -flags low_delay '
            '-max_delay "${ZMK_RTSP_MAX_DELAY_US}" -analyzeduration 0 -probesize 32768 '
            '-i "$ZMK_RTSP_URL" -an -sn -dn -vsync 0 '
            '-flush_packets 1 -f image2pipe -vcodec mjpeg -q:v 5 pipe:1'
        )
        process = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        session.ffmpeg = process
        session.ffmpeg_stderr_task = asyncio.create_task(
            self._drain_ffmpeg_stderr(session, process),
            name=f"ffmpeg-stderr-{session.config.camera_id}",
        )
        session.opened_at = time.monotonic()
        session.received_first_frame = False
        session.failures = 0
        session.decoder_error = ""
        self._log(f"camera {session.config.camera_id} FFmpeg decoder started via {session.transport.upper()}", force=True)

    @staticmethod
    def _decode_jpeg(payload: bytes):
        import numpy as np

        return cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)

    async def _ffmpeg_frame(self, session: CameraSession) -> tuple[Any | None, str]:
        await self._start_ffmpeg(session)
        process = session.ffmpeg
        if process is None or process.stdout is None:
            return None, "FFmpeg decoder не запущен"

        wait_seconds = READ_TIMEOUT_MS / 1000 + 1
        if not session.received_first_frame:
            wait_seconds = max(wait_seconds, KEYFRAME_GRACE_SECONDS)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            buffer = session.ffmpeg_buffer
            start = buffer.find(b"\xff\xd8")
            if start > 0:
                del buffer[:start]
            if start == -1:
                buffer.clear()
            else:
                end = buffer.find(b"\xff\xd9", 2)
                if end != -1:
                    payload = bytes(buffer[:end + 2])
                    del buffer[:end + 2]
                    try:
                        image = await asyncio.to_thread(self._decode_jpeg, payload)
                    except (ImportError, OSError, RuntimeError, ValueError) as exc:
                        return None, redact_error(exc)
                    if image is not None:
                        return image, ""

            remaining = max(0.01, deadline - time.monotonic())
            try:
                chunk = await asyncio.wait_for(process.stdout.read(65_536), timeout=remaining)
            except TimeoutError:
                break
            if not chunk:
                code = process.returncode
                detail = session.decoder_error or "FFmpeg завершил поток без кадра"
                session.opened_at = 0
                session.next_attempt_at = time.monotonic() + RECONNECT_MIN
                session.next_frame_at = max(session.next_frame_at, session.next_attempt_at)
                self._log(
                    f"camera {session.config.camera_id} FFmpeg exited (code {code if code is not None else '?'}): {detail}",
                    force=True,
                )
                self._stop_ffmpeg(session)
                return None, detail
            buffer.extend(chunk)
            if len(buffer) > FFMPEG_FRAME_MAX_BYTES:
                # Retain only the tail after a possible JPEG start marker.
                last_start = buffer.rfind(b"\xff\xd8")
                if last_start >= 0:
                    del buffer[:last_start]
                else:
                    buffer.clear()

        detail = session.decoder_error or "таймаут ожидания кадра от FFmpeg"
        session.opened_at = 0
        session.next_attempt_at = time.monotonic() + RECONNECT_MIN
        session.next_frame_at = max(session.next_frame_at, session.next_attempt_at)
        self._log(f"camera {session.config.camera_id} FFmpeg timeout: {detail}", force=True)
        self._stop_ffmpeg(session)
        return None, detail

    async def _failed_open(self, session: CameraSession, reason: str, latency_ms: int) -> None:
        session.failures += 1
        # A connection-level failure should try the other transport immediately
        # on the next reconnect, not after three identical TCP attempts.
        session.transport_index = (session.transport_index + 1) % len(TRANSPORT_ORDER)
        session.next_attempt_at = time.monotonic() + RECONNECT_MIN
        session.next_frame_at = max(session.next_frame_at, session.next_attempt_at)
        status = "offline" if session.failures >= OFFLINE_AFTER else "recovering"
        await self._report(session, status, latency_ms, reason, force=True)
        self._log(f"camera {session.config.camera_id} open failed: {reason}")

    async def _failed_read(self, session: CameraSession, reason: str, latency_ms: int) -> None:
        now = time.monotonic()
        waiting_for_keyframe = (
            not session.received_first_frame
            and session.opened_at
            and now - session.opened_at < KEYFRAME_GRACE_SECONDS
        )
        if waiting_for_keyframe:
            # H.264 decoding errors immediately after RTSP open are expected
            # when the server starts at a delta frame. Reopening here loses the
            # upcoming IDR frame and creates a permanent black-preview loop.
            session.next_frame_at = max(session.next_frame_at, now + KEYFRAME_RETRY_SECONDS)
            await self._report(
                session,
                "connecting",
                latency_ms,
                "Ожидание первого H.264 keyframe",
                force=session.status != "connecting",
            )
            self._log(f"camera {session.config.camera_id} waiting for first H.264 keyframe")
            return

        session.failures += 1
        status = "offline" if session.failures >= OFFLINE_AFTER else "recovering"
        if session.failures >= OFFLINE_AFTER:
            self._release_session(session)
            session.transport_index = (session.transport_index + 1) % len(TRANSPORT_ORDER)
            session.next_attempt_at = now + RECONNECT_MIN
            session.next_frame_at = max(session.next_frame_at, session.next_attempt_at)
        await self._report(session, status, latency_ms, reason, force=True)
        self._log(f"camera {session.config.camera_id} frame failed: {reason}")

    @staticmethod
    def _encode_snapshot(image: Any) -> bytes | None:
        height, width = image.shape[:2]
        preview = image
        longest = max(width, height)
        if longest > 960:
            scale = 960 / longest
            preview = cv2.resize(image, (max(1, int(width * scale)), max(1, int(height * scale))))
        for quality in (75, 60, 45):
            ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, quality])
            size = getattr(encoded, "nbytes", len(encoded))
            if ok and size <= 1_250_000:
                return bytes(encoded)
        return None

    async def _publish_snapshot(self, session: CameraSession, image: Any) -> None:
        now = time.monotonic()
        if now - session.last_snapshot_at < SNAPSHOT_INTERVAL_SECONDS:
            return
        try:
            encoded = await asyncio.to_thread(self._encode_snapshot, image)
        except (AttributeError, OSError, RuntimeError, ValueError) as exc:
            self._log(f"snapshot encode failed for {session.config.camera_id}: {redact_error(exc)}")
            return
        if not encoded:
            self._log(f"snapshot is too large for {session.config.camera_id}")
            return
        try:
            await self.post_internal(
                f"/api/internal/cameras/{session.config.camera_id}/snapshot",
                {
                    "jpeg_base64": base64.b64encode(encoded).decode(),
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            session.last_snapshot_at = now
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            self._log(f"snapshot upload failed for {session.config.camera_id}: {redact_error(exc)}")

    async def _publish_live(self, session: CameraSession, image: Any) -> None:
        if LIVE_PREVIEW_FPS <= 0:
            return
        now = time.monotonic()
        rate = min(session.config.fps_limit, LIVE_PREVIEW_FPS)
        if rate <= 0 or now - session.last_live_at < 1 / rate:
            return
        try:
            encoded = await asyncio.to_thread(self._encode_snapshot, image)
            if not encoded:
                return
            await self.post_internal_jpeg(f"/api/internal/cameras/{session.config.camera_id}/live-frame", encoded)
            session.last_live_at = now
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            self._log(f"live preview upload failed for {session.config.camera_id}: {redact_error(exc)}")

    async def _publish_event_evidence(self, session: CameraSession, result: Any, image: Any) -> None:
        """Attach the annotated source frame to newly accepted events."""
        if not isinstance(result, dict):
            return
        event_ids={item.get("event_id") for item in result.get("accepted",[]) if isinstance(item,dict) and isinstance(item.get("event_id"),int)}
        if not event_ids:
            return
        try:
            encoded=await asyncio.to_thread(self._encode_snapshot,image)
            if not encoded:
                return
            for event_id in sorted(event_ids):
                await self.post_internal_jpeg(f"/api/internal/events/{event_id}/frame",encoded)
        except (httpx.HTTPError,OSError,RuntimeError,TypeError,ValueError) as exc:
            self._log(f"event evidence upload failed for {session.config.camera_id}: {redact_error(exc)}")

    async def _infer(self, session: CameraSession, image: Any) -> InferenceVisual | None:
        ready=self._ready_models()
        if not ready:
            return None
        stamp = datetime.now(timezone.utc).isoformat()
        shape=(int(image.shape[0]),int(image.shape[1]))
        raw: list[ModelBox] = []
        declared: set[str] = set()
        next_index=0
        primary_succeeded=False
        # Models are deliberately scheduled independently but predictions are
        # serialised on one GPU/CPU context. This avoids VRAM races while still
        # combining person, helmet and workwear detections from separate files.
        async with self._inference_lock:
            for model_name,model,device in ready:
                try:
                    confidence=self.model_test_conf if self.model_test_mode else CONF
                    result=(await asyncio.to_thread(model.predict,image,conf=confidence,device=device,verbose=False))[0]
                    names=getattr(result,"names",None) or getattr(model,"names",{})
                    declared.update(model_semantics(names))
                    values=zip(result.boxes.xyxy.cpu().tolist(),result.boxes.cls.cpu().tolist(),result.boxes.conf.cpu().tolist())
                    for local_index,(xyxy,cls,score) in enumerate(values):
                        if len(xyxy)!=4:
                            continue
                        x1,y1,x2,y2=(float(value) for value in xyxy)
                        if x2<=x1 or y2<=y1:
                            continue
                        label=_label_at(names,int(cls))
                        raw.append(ModelBox((x1,y1,x2,y2),label,normalise_model_label(label),float(score),next_index+local_index,model_name))
                    next_index+=max(1,len(raw)-next_index)
                    if model_name==self.model_name:
                        primary_succeeded=True
                        self._model_error=""
                    for state in self.slot_models.values():
                        if state.info.get("name")==model_name:
                            state.error=""
                except Exception as exc:  # noqa: BLE001 - one model must not stop the others.
                    safe=redact_error(exc)
                    if model_name==self.model_name:
                        self._model_loading_name=model_name; self._model_error=safe
                    for state in self.slot_models.values():
                        if state.info.get("name")==model_name:
                            state.error=safe
                    self._log(f"inference failed for {model_name}; other pipeline models continue: {safe}")
        if not raw and not primary_succeeded and self._model_error:
            return None

        detections: list[dict[str, Any]] = []
        frame_token = int(time.time() * 1000)

        def append_event(event_type: str, evidence: ModelBox, person: ModelBox | None = None, *, inferred: bool = False) -> None:
            anchor = person or evidence
            x1,y1,x2,y2=anchor.bbox
            source_model=evidence.model_name or anchor.model_name or self.model_name
            confidence=evidence.confidence
            person_id=_person_id(session.config.camera_id,anchor) if person else f"{session.config.camera_id}-{event_type}-{int(((x1+x2)/2)//100)}-{source_model}"
            detections.append({
                "camera_id":session.config.camera_id,
                "model_name":source_model,
                "timestamp":stamp,
                "event_type":event_type,
                "confidence":confidence,
                "person_id":person_id,
                "detection_id":f"{session.config.camera_id}:{frame_token}:{source_model}:{evidence.index}:{'derived' if inferred else event_type}",
                "bbox":[x1,y1,x2,y2],
            })

        for box in raw:
            if box.semantic in EVENT_CLASSES and box.semantic not in {"no_helmet","no_vest"}:
                append_event(box.semantic,box)
        helmet_violations=ppe_no_helmet_violations(raw,declared)
        vest_violations=ppe_no_vest_violations(raw,declared)
        for evidence,person,inferred in helmet_violations:
            append_event("no_helmet",evidence,person,inferred=inferred)
        for evidence,person,inferred in vest_violations:
            append_event("no_vest",evidence,person,inferred=inferred)

        try:
            annotated=await asyncio.to_thread(draw_detection_overlay,image,raw,helmet_violations,vest_violations)
        except (OSError,RuntimeError,TypeError,ValueError) as exc:
            self._log(f"overlay render failed; raw preview continues: {redact_error(exc)}")
            annotated=None

        # A requested camera test is deliberately global: no specialised slot
        # may emit production alerts while the operator evaluates a test model.
        if detections and not self.model_test_mode:
            try:
                receipt=await self.post("/api/inference/detections",{"detections":detections})
                await self._publish_event_evidence(session,receipt,annotated if annotated is not None else image)
            except (httpx.HTTPError,OSError,RuntimeError,ValueError) as exc:
                self._log(f"detections rejected: {redact_error(exc)}")
        return InferenceVisual(shape,raw,helmet_violations,vest_violations)

    def _collect_inference_result(self, session: CameraSession) -> None:
        task=session.inference_task
        if task is None or not task.done():
            return
        session.inference_task=None
        try:
            visual=task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - inference must not stop preview.
            self._log(f"inference task failed; fast preview continues: {redact_error(exc)}")
            return
        if visual is not None:
            session.latest_visual=visual

    async def frame(self, camera: dict[str, Any] | CameraConfig) -> None:
        """Read one frame. Scheduling is performed by run(), so tests and
        one-off calls can invoke this method repeatedly without artificial sleep.
        """
        config = camera if isinstance(camera, CameraConfig) else CameraConfig.from_api(camera)
        session = self.sessions.get(config.camera_id)
        if session is None:
            session = CameraSession(config=config)
            self.sessions[config.camera_id] = session
        elif session.config.signature != config.signature:
            self._release_session(session)
            session.config = config
            session.transport_index = 0
            session.failures = 0
            session.next_attempt_at = 0

        started = time.perf_counter()
        if CAMERA_DECODER == "ffmpeg":
            if time.monotonic() < session.next_attempt_at:
                return
            image, error = await self._ffmpeg_frame(session)
            ok = image is not None
        else:
            if not await self._open(session):
                return
            try:
                # CAP_PROP_READ_TIMEOUT_MSEC was supplied before open(). Await
                # the native call instead of cancelling it mid-FFmpeg-read.
                ok, image = await asyncio.to_thread(session.capture.read)
            except Exception as exc:  # noqa: BLE001 - OpenCV exception types vary by build.
                ok, image = False, None
                error = redact_error(exc)
            else:
                error = "пустой кадр от RTSP-потока"
        latency = round((time.perf_counter() - started) * 1000)

        if not ok or image is None:
            await self._failed_read(session, error, latency)
            return

        session.failures = 0
        session.next_attempt_at = 0
        if not session.received_first_frame:
            self._log(f"camera {session.config.camera_id} received first decoded frame", force=True)
        session.received_first_frame = True
        session.frames_in_window += 1
        await self._report(session, "online", latency, "", force=session.status != "online")
        # Publish the decoded source frame immediately. The latest completed
        # detector boxes are painted onto matching-size frames without waiting
        # for a new YOLO pass, so preview FPS follows the RTSP source rather
        # than the much slower inference cadence.
        self._collect_inference_result(session)
        preview=image
        visual=session.latest_visual
        try:
            if visual is not None and visual.shape==tuple(image.shape[:2]):
                preview=draw_detection_overlay(image,visual.boxes,visual.helmet_violations,visual.vest_violations) or image
        except (AttributeError,TypeError,ValueError):
            preview=image
        await self._publish_live(session, preview)
        await self._publish_snapshot(session, preview)

        now=time.monotonic()
        if self._ready_models() and session.inference_task is None and now>=session.next_inference_at:
            try:
                inference_image=image.copy()
            except AttributeError:
                inference_image=image
            session.next_inference_at=now+1/INFERENCE_FPS
            session.inference_task=asyncio.create_task(
                self._infer(session,inference_image),
                name=f"camera-inference-{session.config.camera_id}",
            )

    async def run(self) -> None:
        print(
            f"inference: camera runtime started (api={API}, device={DEVICE_SETTING}, transport={RTSP_TRANSPORT}, decoder={CAMERA_DECODER})",
            flush=True,
        )
        try:
            while True:
                if not _worker_token():
                    self._log("waiting for worker token", force=True)
                    await asyncio.sleep(2)
                    continue

                now = time.monotonic()
                try:
                    if now >= self._next_control_poll:
                        await self._refresh_control()
                        self._next_control_poll = time.monotonic() + CONTROL_POLL_SECONDS

                    for session in list(self.sessions.values()):
                        self._collect_inference_result(session)
                        task = session.frame_task
                        if task is not None:
                            if not task.done():
                                continue
                            try:
                                task.result()
                            except asyncio.CancelledError:
                                pass
                            except Exception as exc:  # noqa: BLE001 - isolate one camera task.
                                self._log(f"camera {session.config.camera_id} task failed: {redact_error(exc)}")
                            session.frame_task = None
                            if session.restart_pending:
                                self._release_session(session)
                                session.restart_pending = False

                        now = time.monotonic()
                        if now < session.next_frame_at:
                            continue
                        session.next_frame_at = now + 1 / session.config.fps_limit
                        session.frame_task = asyncio.create_task(
                            self.frame(session.config),
                            name=f"camera-frame-{session.config.camera_id}",
                        )

                    self._cleanup_deferred_releases()
                    await self._heartbeat()
                    wake_times = [self._next_control_poll, self._last_heartbeat_at + HEARTBEAT_INTERVAL_SECONDS]
                    for session in self.sessions.values():
                        if session.frame_task is not None and not session.frame_task.done():
                            continue
                        wake_times.append(session.next_frame_at)
                        if session.next_attempt_at:
                            wake_times.append(session.next_attempt_at)
                    delay = min(wake_times) - time.monotonic() if wake_times else 1.0
                    await asyncio.sleep(min(0.25, max(0.01, delay)))
                except Exception as exc:  # noqa: BLE001 - a service worker must self-heal.
                    self._log(f"control loop error: {redact_error(exc)}", force=True)
                    self._next_control_poll = time.monotonic() + 2
                    await asyncio.sleep(1)
        finally:
            # Do not cancel/release a native OpenCV call in flight. Docker
            # process termination is safer than racing FFmpeg from Python.
            for session in [*self.sessions.values(), *self._deferred_releases]:
                if session.inference_task is not None and not session.inference_task.done():
                    session.inference_task.cancel()
                if self._frame_task_finished(session):
                    self._release_session(session)
            if self._model_task is not None and not self._model_task.done():
                self._model_task.cancel()
            if self._http is not None:
                await self._http.aclose()


if __name__ == "__main__":
    asyncio.run(Runtime().run())
