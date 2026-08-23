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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import httpx

API = os.getenv("ZMK_API_URL", "http://api:8000").rstrip("/")
API_KEY = os.getenv("ZMK_API_KEY", "")
DEVICE_SETTING = os.getenv("INFERENCE_DEVICE", "auto").strip() or "auto"


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
    return str(info["name"]), _yolo_class()(str(path)), device


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
            fps_limit=max(0.1, float(raw.get("fps_limit") or 8)),
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
    last_error: str = ""
    frame_task: asyncio.Task[None] | None = None

    @property
    def transport(self) -> str:
        return TRANSPORT_ORDER[self.transport_index % len(TRANSPORT_ORDER)]


class Runtime:
    """Owns camera sessions, API protocol and optional model inference."""

    def __init__(self) -> None:
        self.sessions: dict[str, CameraSession] = {}
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
        self.device = DEVICE_SETTING
        self._model_task: asyncio.Task[tuple[str, Any, str]] | None = None
        self._model_loading_name = ""
        self._model_retry_at = 0.0
        self._no_model_announced = False

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
        return await self._request("POST", path, data)

    async def post_internal(self, path: str, data: dict[str, Any]) -> Any:
        return await self._request("POST", path, data, internal=True)

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

    def _release_session(self, session: CameraSession) -> None:
        self._release(session.capture)
        session.capture = None

    @staticmethod
    def _cancel_frame_task(session: CameraSession) -> None:
        if session.frame_task is not None and not session.frame_task.done():
            session.frame_task.cancel()
        session.frame_task = None

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

    async def _heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat_at < HEARTBEAT_INTERVAL_SECONDS:
            return
        status = "running" if self.sessions else "idle"
        detail = f"cameras={len(self.sessions)} model={self.model_name or 'none'}"
        try:
            await self.post_internal(
                "/api/internal/inference/heartbeat",
                {"status": status, "detail": detail, "camera_count": len(self.sessions)},
            )
            self._last_heartbeat_at = now
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            self._log(f"heartbeat rejected: {redact_error(exc)}")

    def _sync_cameras(self, raw_cameras: list[dict[str, Any]]) -> None:
        desired = {config.camera_id: config for config in map(CameraConfig.from_api, raw_cameras)}
        for camera_id in set(self.sessions) - set(desired):
            session = self.sessions.pop(camera_id)
            self._cancel_frame_task(session)
            self._release_session(session)
            self._transport_cursor.pop(camera_id, None)

        for camera_id, config in desired.items():
            session = self.sessions.get(camera_id)
            if session is None:
                self.sessions[camera_id] = CameraSession(config=config)
                continue
            if session.config.signature != config.signature:
                self._cancel_frame_task(session)
                self._release_session(session)
                session.config = config
                session.status = "connecting"
                session.transport_index = 0
                session.failures = 0
                session.next_attempt_at = 0
                session.next_frame_at = 0
                session.last_error = ""
            else:
                session.config = config

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
            if not self._no_model_announced:
                self._log("no active model; camera preview and telemetry remain enabled", force=True)
                self._no_model_announced = True
            return

        wanted_name = str(info["name"])
        self._no_model_announced = False
        if self.model_name == wanted_name and self.model is not None:
            return

        if self._model_task is not None:
            if not self._model_task.done():
                return
            task = self._model_task
            self._model_task = None
            try:
                loaded_name, model, device = task.result()
            except (OSError, RuntimeError, ValueError, ImportError) as exc:
                self._model_retry_at = now + 15
                self._log(f"model unavailable; camera capture continues: {redact_error(exc)}", force=True)
                return
            if loaded_name == wanted_name:
                self.model = model
                self.model_name = loaded_name
                self.device = device
                self._log(f"active model loaded: {loaded_name} (device={device})", force=True)
                return

        if now < self._model_retry_at:
            return
        self.model = None
        self.model_name = ""
        self._model_loading_name = wanted_name
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
            info = await self.get("/api/internal/active-model", internal=True)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            self._log(f"active-model query failed; camera capture continues: {redact_error(exc)}")
            info = None
        await self._refresh_model(info)

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
                capture = await asyncio.wait_for(
                    asyncio.to_thread(self._open_capture, session.config.rtsp_url, transport),
                    timeout=OPEN_TIMEOUT_MS / 1000 + 2,
                )
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
        self._log(f"camera {session.config.camera_id} opened via {transport.upper()}", force=True)
        return True

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
        session.failures += 1
        status = "offline" if session.failures >= OFFLINE_AFTER else "recovering"
        if session.failures >= OFFLINE_AFTER:
            self._release_session(session)
            session.transport_index = (session.transport_index + 1) % len(TRANSPORT_ORDER)
            session.next_attempt_at = time.monotonic() + RECONNECT_MIN
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

    async def _infer(self, session: CameraSession, image: Any) -> None:
        if self.model is None or not self.model_name:
            return
        try:
            # GPU/Ultralytics inference is intentionally serialised; RTSP
            # capture itself stays concurrent and does not wait for a stalled
            # camera opening or read.
            async with self._inference_lock:
                result = (
                    await asyncio.to_thread(
                        self.model.predict,
                        image,
                        conf=CONF,
                        device=self.device,
                        verbose=False,
                    )
                )[0]
        except Exception as exc:  # noqa: BLE001 - ML failures must not stop RTSP.
            self._log(f"inference failed; camera capture continues: {redact_error(exc)}")
            return

        detections: list[dict[str, Any]] = []
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            values = zip(
                result.boxes.xyxy.cpu().tolist(),
                result.boxes.cls.cpu().tolist(),
                result.boxes.conf.cpu().tolist(),
            )
            for index, (xyxy, cls, score) in enumerate(values):
                label = str(self.model.names[int(cls)])
                if label not in EVENT_CLASSES:
                    continue
                x1, y1, x2, y2 = xyxy
                detections.append(
                    {
                        "camera_id": session.config.camera_id,
                        "model_name": self.model_name,
                        "timestamp": stamp,
                        "event_type": label,
                        "confidence": score,
                        "person_id": f"{session.config.camera_id}-{label}-{int(((x1 + x2) / 2) // 100)}-{int(((y1 + y2) / 2) // 100)}",
                        "detection_id": f"{session.config.camera_id}:{int(time.time() * 1000)}:{index}",
                        "bbox": [x1, y1, x2, y2],
                    }
                )
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            self._log(f"invalid model output ignored: {redact_error(exc)}")
            return

        if detections:
            try:
                await self.post("/api/inference/detections", {"detections": detections})
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                self._log(f"detections rejected: {redact_error(exc)}")

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

        if not await self._open(session):
            return

        started = time.perf_counter()
        try:
            ok, image = await asyncio.wait_for(
                asyncio.to_thread(session.capture.read),
                timeout=READ_TIMEOUT_MS / 1000 + 1,
            )
        except TimeoutError:
            ok, image = False, None
            error = "таймаут чтения RTSP-кадра"
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
        session.frames_in_window += 1
        await self._report(session, "online", latency, "", force=session.status != "online")
        await self._publish_snapshot(session, image)
        await self._infer(session, image)

    async def run(self) -> None:
        print(
            f"inference: camera runtime started (api={API}, device={DEVICE_SETTING}, transport={RTSP_TRANSPORT})",
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

                        now = time.monotonic()
                        if now < session.next_frame_at:
                            continue
                        session.next_frame_at = now + 1 / session.config.fps_limit
                        session.frame_task = asyncio.create_task(
                            self.frame(session.config),
                            name=f"camera-frame-{session.config.camera_id}",
                        )

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
            pending = []
            for session in self.sessions.values():
                if session.frame_task is not None and not session.frame_task.done():
                    session.frame_task.cancel()
                    pending.append(session.frame_task)
                self._release_session(session)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self._model_task is not None and not self._model_task.done():
                self._model_task.cancel()
            if self._http is not None:
                await self._http.aclose()


if __name__ == "__main__":
    asyncio.run(Runtime().run())
