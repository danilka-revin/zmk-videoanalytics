from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import httpx
import torch
from ultralytics import YOLO

API = os.getenv("ZMK_API_URL", "http://api:8000").rstrip("/")
API_KEY = os.getenv("ZMK_API_KEY", "")
DEVICE_SETTING = os.getenv("INFERENCE_DEVICE", "auto")
DEVICE = ("0" if torch.cuda.is_available() else "cpu") if DEVICE_SETTING == "auto" else DEVICE_SETTING


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


CONF = _bounded_float("INFERENCE_CONF", 0.5, 0.01, 1.0)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer environment variable without making the worker crash.

    Camera deployments are commonly configured through a hand-edited .env.
    A typo in an operational timeout must fall back to a safe value rather
    than preventing every RTSP stream from starting.
    """
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _worker_token() -> str:
    token = os.getenv("ZMK_WORKER_TOKEN", "").strip()
    if token:
        return token
    token_file = Path(os.getenv("ZMK_WORKER_TOKEN_FILE", "/models/.worker-token"))
    try:
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
    except OSError:
        pass
    return ""


# Kept as a module-level value for backwards compatibility and diagnostics;
# internal requests deliberately call _worker_token() again to handle a token
# provisioned by the API after this container has started.
WORKER_TOKEN = _worker_token()

# "auto" tries TCP first, then alternates to UDP after a failed connection.
# VLC often silently chooses another transport, which is why an explicit
# fallback is important for cameras that work in VLC but not in OpenCV.
RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "auto").lower()
if RTSP_TRANSPORT not in ("auto", "tcp", "udp"):
    RTSP_TRANSPORT = "auto"
TRANSPORT_ORDER = ["tcp", "udp"] if RTSP_TRANSPORT == "auto" else [RTSP_TRANSPORT]

# OpenCV passes FFmpeg options as key;value pairs joined with |, not commas.
def _buffer_size() -> str:
    raw = os.getenv("RTSP_BUFFER_SIZE", "").strip()
    if not raw:
        return ""
    try:
        value = int(raw)
    except ValueError:
        return ""
    return str(max(1, min(1_000_000, value)))


_RTSP_BUFSIZE = _buffer_size()
_RTSP_STIMEOUT = _bounded_int("RTSP_STIMEOUT", 5_000_000, 100_000, 120_000_000)
OFFLINE_AFTER = _bounded_int("OFFLINE_AFTER_FRAMES", 3, 1, 100)
RECONNECT_MIN = _bounded_int("RTSP_RECONNECT_SECONDS", 5, 0, 3_600)

TELEMETRY_INTERVAL_SECONDS = 10.0
SNAPSHOT_INTERVAL_SECONDS = 5.0
CONTROL_POLL_INTERVAL_SECONDS = 2.0
EVENT_CLASSES = {
    "no_helmet",
    "no_vest",
    "phone_usage",
    "smoking",
    "restricted_zone",
    "immobility",
}


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class Runtime:
    def __init__(self) -> None:
        self.model = None
        self.model_name = ""
        self.captures: dict[str, object] = {}
        self.last_telemetry: dict[str, float] = {}
        self.last_status: dict[str, str] = {}
        self.last_snapshot: dict[str, float] = {}
        self.frame_counts: dict[str, int] = {}
        self.last_error: dict[str, float] = {}
        self.fail_counts: dict[str, int] = {}
        self.next_open: dict[str, float] = {}
        self.next_frame: dict[str, float] = {}
        self.transport: dict[str, str] = {}
        self.open_attempts: dict[str, int] = {}
        self.last_camera_ids: tuple[str, ...] = ()
        self.last_no_camera_log = 0.0
        self.no_model_announced = False

    async def get(self, path: str, internal: bool = False):
        if internal:
            # Never cache this value: the API may provision or rotate the
            # shared token after the worker process starts.
            headers = {"X-Worker-Token": _worker_token()}
        else:
            headers = {"X-API-Key": API_KEY} if API_KEY else {}
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            response = await client.get(API + path)
            response.raise_for_status()
            return response.json()

    async def post(self, path: str, data: dict):
        headers = {"X-API-Key": API_KEY} if API_KEY else {}
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            response = await client.post(API + path, json=data)
            response.raise_for_status()
            return response.json()

    async def load_model(self) -> None:
        info = await self.get("/api/internal/active-model", internal=True)
        if not info:
            if not self.no_model_announced:
                print(
                    "inference: no active model; camera preview and telemetry remain enabled",
                    flush=True,
                )
                self.no_model_announced = True
            self.model = None
            self.model_name = ""
            return
        if info["name"] == self.model_name:
            return

        artifact = info["artifact_uri"].removeprefix("file://")
        path = Path(artifact)
        if not path.exists():
            raise RuntimeError(f"Model artifact not found: {artifact}")
        if info.get("checksum"):
            digest = await asyncio.to_thread(file_sha256, path)
            if digest.lower() != info["checksum"].lower():
                raise RuntimeError("Model checksum mismatch")
        self.model = await asyncio.to_thread(YOLO, str(path))
        self.model_name = info["name"]
        self.no_model_announced = False
        print(f"inference: active model loaded: {self.model_name}", flush=True)

    def _next_transport(self, camera_id: str) -> str:
        """Return TCP on the first auto attempt, then rotate on reconnect."""
        current = self.transport.get(camera_id)
        if current is None:
            transport = TRANSPORT_ORDER[0]
        else:
            try:
                index = TRANSPORT_ORDER.index(current)
            except ValueError:
                index = -1
            transport = TRANSPORT_ORDER[(index + 1) % len(TRANSPORT_ORDER)]
        self.transport[camera_id] = transport
        return transport

    def _open_capture(self, url: str, transport: str):
        parts = [f"rtsp_transport;{transport}", f"stimeout;{_RTSP_STIMEOUT}"]
        if _RTSP_BUFSIZE:
            parts.append(f"buffer_size;{_RTSP_BUFSIZE}")
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(parts)

        capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000)
        capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000)
        return capture

    @staticmethod
    def _release(capture: object | None) -> None:
        if capture is None:
            return
        try:
            capture.release()  # type: ignore[attr-defined]
        except (AttributeError, RuntimeError, OSError, ValueError):
            pass

    def _release_capture(self, camera_id: str) -> None:
        self._release(self.captures.pop(camera_id, None))

    @staticmethod
    def _capture_is_open(capture: object | None) -> bool:
        if capture is None:
            return False
        try:
            return bool(capture.isOpened())  # type: ignore[attr-defined]
        except (AttributeError, RuntimeError, OSError, ValueError):
            return False

    def _log_error(self, camera_id: str, message: str, now: float) -> None:
        if now - self.last_error.get(camera_id, 0) > 15:
            print(message, flush=True)
            self.last_error[camera_id] = now

    async def _report_telemetry(
        self,
        camera_id: str,
        status: str,
        latency_ms: int,
        now: float,
        *,
        force: bool = False,
    ) -> None:
        previous_at = self.last_telemetry.get(camera_id)
        previous_status = self.last_status.get(camera_id)
        if (
            not force
            and previous_at is not None
            and previous_status == status
            and now - previous_at < TELEMETRY_INTERVAL_SECONDS
        ):
            return

        elapsed = max(0.001, now - previous_at) if previous_at is not None else 0.0
        frames = self.frame_counts.get(camera_id, 0)
        fps = round(frames / elapsed, 2) if elapsed else 0.0
        try:
            await self.post(
                f"/api/cameras/{camera_id}/telemetry",
                {"status": status, "fps": fps, "latency_ms": latency_ms},
            )
        except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
            self._log_error(
                camera_id,
                f"inference: telemetry for {camera_id} was rejected: {exc}",
                now,
            )
        finally:
            # Keep reporting bounded even when the API is briefly unavailable.
            self.last_telemetry[camera_id] = now
            self.last_status[camera_id] = status
            self.frame_counts[camera_id] = 0

    async def _publish_snapshot(self, camera: dict, image: object, now: float) -> None:
        camera_id = camera["id"]
        if now - self.last_snapshot.get(camera_id, 0) < SNAPSHOT_INTERVAL_SECONDS:
            return

        try:
            height, width = image.shape[:2]  # type: ignore[attr-defined]
            preview = image
            # Fit the longest edge, not only width: portrait cameras otherwise
            # create a 960×very-tall JPEG that can exceed the API size cap.
            longest_edge = max(width, height)
            if longest_edge > 960:
                scale = 960 / longest_edge
                preview = cv2.resize(image, (max(1, int(width * scale)), max(1, int(height * scale))))
            encoded_ok, encoded = False, b""
            # The API deliberately caps snapshots so one noisy 4K frame cannot
            # exhaust storage or request memory. Lower quality before giving up.
            for quality in (75, 60, 45):
                encoded_ok, encoded = cv2.imencode(
                    ".jpg",
                    preview,
                    [cv2.IMWRITE_JPEG_QUALITY, quality],
                )
                encoded_size = getattr(encoded, "nbytes", len(encoded))
                if encoded_ok and encoded_size <= 1_250_000:
                    break
                encoded_ok = False
        except (AttributeError, RuntimeError, OSError, ValueError) as exc:
            self._log_error(camera_id, f"inference: could not encode snapshot for {camera_id}: {exc}", now)
            return

        if not encoded_ok:
            self._log_error(camera_id, f"inference: snapshot for {camera_id} is too large", now)
            return
        try:
            await self.post(
                f"/api/cameras/{camera_id}/snapshot",
                {
                    "jpeg_base64": base64.b64encode(encoded).decode(),
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
            # Do not advance last_snapshot: a transient API failure should be
            # retried on the next frame rather than leaving a black card.
            self._log_error(camera_id, f"inference: snapshot for {camera_id} was rejected: {exc}", now)
            return
        self.last_snapshot[camera_id] = now

    async def _record_failure(
        self,
        camera: dict,
        now: float,
        latency_ms: int,
        *,
        reason: str,
    ) -> None:
        camera_id = camera["id"]
        failures = self.fail_counts.get(camera_id, 0) + 1
        self.fail_counts[camera_id] = failures
        status = "offline" if failures >= OFFLINE_AFTER else "recovering"
        if failures >= OFFLINE_AFTER:
            self._release_capture(camera_id)
            self.next_open[camera_id] = now + RECONNECT_MIN

        await self._report_telemetry(
            camera_id,
            status,
            latency_ms,
            now,
            force=status != self.last_status.get(camera_id),
        )
        self._log_error(
            camera_id,
            (
                f"inference: camera {camera_id}: {reason} ({camera['name']}); "
                f"failures={failures}, transport={self.transport.get(camera_id, '?')}"
            ),
            now,
        )

    async def frame(self, camera: dict) -> None:
        camera_id = camera["id"]
        now = time.time()
        capture = self.captures.get(camera_id)

        if not self._capture_is_open(capture):
            if now < self.next_open.get(camera_id, 0):
                # The scheduler will wake this camera once its reconnect time
                # arrives. Never sleep here: one offline camera must not stall
                # every other camera in the single worker loop.
                return
            self._release_capture(camera_id)
            transport = self._next_transport(camera_id)
            self.open_attempts[camera_id] = self.open_attempts.get(camera_id, 0) + 1
            opened_at = time.perf_counter()
            try:
                capture = self._open_capture(camera["rtsp_url"], transport)
            except (RuntimeError, OSError, ValueError) as exc:
                await self._record_failure(
                    camera,
                    now,
                    round((time.perf_counter() - opened_at) * 1000),
                    reason=f"could not start capture: {exc}",
                )
                self.next_open[camera_id] = now + RECONNECT_MIN
                return

            if not self._capture_is_open(capture):
                self._release(capture)
                self.next_open[camera_id] = now + RECONNECT_MIN
                await self._record_failure(
                    camera,
                    now,
                    round((time.perf_counter() - opened_at) * 1000),
                    reason=f"failed to open via {transport}",
                )
                return

            self.captures[camera_id] = capture
            print(
                f"inference: camera {camera_id}: opened via {transport} ({camera['name']})",
                flush=True,
            )

        started = time.perf_counter()
        try:
            ok, image = await asyncio.to_thread(capture.read)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - OpenCV raises implementation-specific errors.
            ok, image = False, None
            read_error = str(exc)
        else:
            read_error = "stream read failed"
        latency = round((time.perf_counter() - started) * 1000)

        if not ok or image is None:
            await self._record_failure(camera, now, latency, reason=read_error)
            return

        had_failures = self.fail_counts.get(camera_id, 0) > 0
        self.fail_counts[camera_id] = 0
        self.next_open.pop(camera_id, None)
        self.last_error.pop(camera_id, None)
        self.frame_counts[camera_id] = self.frame_counts.get(camera_id, 0) + 1
        await self._report_telemetry(
            camera_id,
            "online",
            latency,
            now,
            force=had_failures or self.last_status.get(camera_id) != "online",
        )

        # A live preview is a camera feature, not an AI-model feature. Publish
        # it before the model guard so operators can diagnose an RTSP stream
        # while no active model has been registered yet.
        await self._publish_snapshot(camera, image, now)
        if self.model is None:
            return

        result = (
            await asyncio.to_thread(
                self.model.predict,
                image,
                conf=CONF,
                device=DEVICE,
                verbose=False,
            )
        )[0]
        detections = []
        stamp = datetime.now(timezone.utc).isoformat()
        for index, (xyxy, cls, score) in enumerate(
            zip(
                result.boxes.xyxy.cpu().tolist(),
                result.boxes.cls.cpu().tolist(),
                result.boxes.conf.cpu().tolist(),
            )
        ):
            label = str(self.model.names[int(cls)])
            if label not in EVENT_CLASSES:
                continue
            x1, y1, x2, y2 = xyxy
            spatial_id = (
                f"{camera_id}-{label}-{int(((x1 + x2) / 2) // 100)}-"
                f"{int(((y1 + y2) / 2) // 100)}"
            )
            detections.append(
                {
                    "camera_id": camera_id,
                    "model_name": self.model_name,
                    "timestamp": stamp,
                    "event_type": label,
                    "confidence": score,
                    "person_id": spatial_id,
                    "detection_id": f"{camera_id}:{int(time.time() * 1000)}:{index}",
                    "bbox": [x1, y1, x2, y2],
                }
            )
        if detections:
            try:
                await self.post("/api/inference/detections", {"detections": detections})
            except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
                self._log_error(camera_id, f"inference: detections were rejected: {exc}", now)

    def _drop_stale_cameras(self, active_ids: set[str]) -> None:
        for camera_id in set(self.captures) - active_ids:
            self._release_capture(camera_id)
            self.last_telemetry.pop(camera_id, None)
            self.last_status.pop(camera_id, None)
            self.last_snapshot.pop(camera_id, None)
            self.frame_counts.pop(camera_id, None)
            self.last_error.pop(camera_id, None)
            self.fail_counts.pop(camera_id, None)
            self.next_open.pop(camera_id, None)
            self.next_frame.pop(camera_id, None)
            self.transport.pop(camera_id, None)
            self.open_attempts.pop(camera_id, None)

    async def run(self) -> None:
        cameras: list[dict] = []
        next_control_poll = 0.0
        print(
            f"inference: started (api={API}, device={DEVICE}, transport={RTSP_TRANSPORT})",
            flush=True,
        )
        try:
            while True:
                # The API owns provisioning of the shared secret. Wait for it
                # instead of exiting permanently during a container start race.
                if not _worker_token():
                    print("inference: waiting for worker token", flush=True)
                    await asyncio.sleep(2)
                    continue
                try:
                    now = time.time()
                    if now >= next_control_poll:
                        # Poll the control plane at a bounded rate, then run
                        # camera schedules independently. The old `sleep` in a
                        # per-camera loop divided each camera's effective FPS
                        # by the number of configured cameras.
                        await self.load_model()
                        cameras = await self.get("/api/internal/cameras", internal=True)
                        camera_ids = tuple(camera["id"] for camera in cameras)
                        self._drop_stale_cameras(set(camera_ids))
                        if camera_ids != self.last_camera_ids:
                            if camera_ids:
                                print(
                                    f"inference: monitoring {len(camera_ids)} camera(s): {', '.join(camera_ids)}",
                                    flush=True,
                                )
                            self.last_camera_ids = camera_ids
                        if not camera_ids and now - self.last_no_camera_log >= 30:
                            print(
                                "inference: no enabled RTSP cameras returned by API; "
                                "set RTSP_CAM_01 or add and enable a camera in the web panel",
                                flush=True,
                            )
                            self.last_no_camera_log = now
                        next_control_poll = time.time() + CONTROL_POLL_INTERVAL_SECONDS

                    if not cameras:
                        await asyncio.sleep(min(1.0, max(0.05, next_control_poll - time.time())))
                        continue

                    wake_at = next_control_poll
                    for camera in cameras:
                        camera_id = camera["id"]
                        due_at = max(
                            self.next_frame.get(camera_id, 0),
                            self.next_open.get(camera_id, 0),
                        )
                        now = time.time()
                        if now < due_at:
                            wake_at = min(wake_at, due_at)
                            continue

                        started = now
                        await self.frame(camera)
                        try:
                            interval = max(0.01, 1 / float(camera["fps_limit"]))
                        except (TypeError, ValueError, ZeroDivisionError):
                            interval = 0.125
                        self.next_frame[camera_id] = max(
                            started + interval,
                            self.next_open.get(camera_id, 0),
                        )
                        wake_at = min(wake_at, self.next_frame[camera_id])

                    await asyncio.sleep(min(0.2, max(0.01, wake_at - time.time())))
                except Exception as exc:  # noqa: BLE001 - keep a long-running worker alive.
                    print(f"inference loop error: {exc}", flush=True)
                    cameras = []
                    next_control_poll = 0.0
                    await asyncio.sleep(3)
        finally:
            for camera_id in list(self.captures):
                self._release_capture(camera_id)


if __name__ == "__main__":
    asyncio.run(Runtime().run())
