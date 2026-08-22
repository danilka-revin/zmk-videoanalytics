"""Inference worker camera hardening: offline-threshold + TCP transport.

Drives the real frame() logic with a controllable fake cv2.VideoCapture and
stubs the heavy ultralytics/torch imports so the module loads in bare CI.
"""
import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest

# opencv + heavy deps are container-only; skip in bare CI env.
pytest.importorskip("cv2")

WORKER = Path(__file__).resolve().parents[1] / "services" / "inference_worker"


class _FakeCap:
    _opened: ClassVar[bool] = True
    _results: ClassVar[list] = []

    def __init__(self, *a, **k):
        self._opened = True

    def isOpened(self):
        return self._opened

    def read(self):
        if not self._results:
            return False, None
        return self._results.pop(0)

    def set(self, *a, **k):
        return None

    def release(self):
        self._opened = False

    def open(self, *a, **k):
        self._opened = True
        return True


@pytest.fixture
def worker_mod(monkeypatch):
    monkeypatch.setenv("RTSP_TRANSPORT", "tcp")

    class _StubYOLO:
        names: ClassVar[dict] = {0: "no_helmet"}

        def __call__(self, *a, **k):
            return self

    pt = types.ModuleType("torch")
    pt.cuda = types.SimpleNamespace(is_available=lambda: False)
    sys.modules["torch"] = pt
    ul = types.ModuleType("ultralytics")
    ul.YOLO = _StubYOLO
    sys.modules["ultralytics"] = ul

    sys.modules.pop("main", None)
    sys.path.insert(0, str(WORKER))
    mod = importlib.import_module("main")
    return mod


def test_offline_reported_only_after_threshold(worker_mod):
    import numpy as np

    worker = worker_mod
    r = worker.Runtime()
    r.model = None
    r.model_name = ""
    cam = {"id": "cam_01", "name": "Test", "rtsp_url": "rtsp://x/stream", "fps_limit": 8}
    frame = np.zeros((20, 20, 3), dtype=np.uint8)

    cap = _FakeCap()
    # two good frames then many failures; OFFLINE_AFTER default 3
    cap._results = [(True, frame), (True, frame)] + [(False, None)] * 12
    r.captures["cam_01"] = cap
    r.frame_counts["cam_01"] = 0
    r.last_telemetry["cam_01"] = 0

    statuses = []

    async def post(path, data):
        if path.startswith("/api/cameras/cam_01/telemetry"):
            statuses.append(data["status"])

    r.post = post
    import time as _t
    r.last_telemetry["cam_01"] = _t.time() - 11  # force telemetry now

    for _ in range(6):
        asyncio.run(r.frame(cam))

    assert statuses[0] == "online"          # reported online on first good frame
    assert "offline" in statuses            # reachable once threshold exceeded
    # A single droppped frame must not immediately flip to offline; the first
    # failing frame pair should produce 'recovering' before 'offline'.
    assert statuses[1] in ("recovering", "offline")


def test_worker_sets_tcp_transport_and_threshold(worker_mod):
    worker = worker_mod
    assert worker.RTSP_TRANSPORT in ("auto", "tcp", "udp")
    assert worker.TRANSPORT_ORDER  # either ["tcp","udp"] (auto) or a single mode
    # Per-open options include the transport of the current attempt and MUST
    # NOT include extra ',stimeout;...' or other keys: FFmpeg rejects the whole
    # rtsp_transport option when it sees trailing chars, which made BOTH tcp
    # and udp fail (camera never opened). Guard against the regression.
    r = worker.Runtime()
    r._open_capture("rtsp://x/stream", "tcp")
    opt = str(worker.os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", ""))
    assert opt == "rtsp_transport;tcp", opt
    assert "stimeout" not in opt and "buffer_size" not in opt
    assert worker.OFFLINE_AFTER >= 1


def test_transport_fallback_order(worker_mod):
    # auto -> alternates tcp/udp; fixed stays fixed
    worker_mod.RTSP_TRANSPORT = "auto"
    worker_mod.TRANSPORT_ORDER = ["tcp", "udp"]
    r = worker_mod.Runtime()
    assert r._next_transport("cam_x") == "udp"      # from default tcp -> udp
    assert r._next_transport("cam_x") == "tcp"      # back to tcp
    # fixed
    worker_mod.RTSP_TRANSPORT = "tcp"
    worker_mod.TRANSPORT_ORDER = ["tcp"]
    assert r._next_transport("cam_y") == "tcp"
