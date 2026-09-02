"""Зеркалирование журналов отдельных процессов в единый журнал проекта.

Training worker и updater не имеют доступа к docker logs из браузера, поэтому
они буферизуют свои строки и отдают их в POST /api/service-logs. Здесь
проверяется сам механизм: уровни, буфер, повторная постановка в очередь при
недоступном API и отсутствие фонового обмена, когда API не настроен.
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


class _FakeResponse:
    status_code = 202

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in that records posted payloads."""

    def __init__(self, error: Exception | None = None) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.error = error

    async def post(self, path: str, json: dict):
        if self.error is not None:
            raise self.error
        self.posts.append((path, json))
        return _FakeResponse()


def _stub_heavy_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    cv2 = types.ModuleType("cv2")
    cv2.VideoCapture = object
    cv2.imwrite = lambda *args, **kwargs: True
    cv2.imread = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch)
    ultralytics = types.ModuleType("ultralytics")
    ultralytics.__path__ = []
    ultralytics.YOLO = object
    monkeypatch.setitem(sys.modules, "ultralytics", ultralytics)


def _load_module(monkeypatch: pytest.MonkeyPatch, relative: str, name: str):
    path = ROOT / relative
    monkeypatch.syspath_prepend(str(path.parent))
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def training_worker(monkeypatch: pytest.MonkeyPatch):
    _stub_heavy_modules(monkeypatch)
    module = _load_module(monkeypatch, "services/training_worker/main.py", "zmk_training_worker_under_test")
    original_stdout, original_stderr = sys.stdout, sys.stderr
    module._log_ship_lines.clear()
    yield module
    module._log_ship_lines.clear()
    sys.stdout, sys.stderr = original_stdout, original_stderr
    sys.modules.pop("zmk_training_worker_under_test", None)


def test_training_worker_ships_printed_lines_and_explicit_events(training_worker):
    training_worker.install_log_shipping()
    assert isinstance(sys.stdout, training_worker._LogShippingStream)
    # Прямая печать worker-а и явные события задачи попадают в один буфер.
    print("ultralytics: RuntimeError: CUDA out of memory")
    print("epoch 3/40 finished")
    training_worker.ship_log("job 12 started: target=helmet-v3 epochs=40", "INFO")
    training_worker.ship_log("job 12 failed: dataset is empty", "ERROR")

    client = _FakeClient()
    asyncio.run(training_worker.ship_logs(client))

    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/api/service-logs" and payload["service"] == "training"
    levels = {entry["message"]: entry["level"] for entry in payload["entries"]}
    assert levels["ultralytics: RuntimeError: CUDA out of memory"] == "ERROR"
    assert levels["epoch 3/40 finished"] == "INFO"
    assert levels["job 12 started: target=helmet-v3 epochs=40"] == "INFO"
    assert levels["job 12 failed: dataset is empty"] == "ERROR"
    assert len(training_worker._log_ship_lines) == 0


def test_training_worker_requeues_lines_when_the_api_is_unreachable(training_worker):
    training_worker.ship_log("job 13 progress: epoch 3/40")
    asyncio.run(training_worker.ship_logs(_FakeClient(httpx.ConnectError("api is down"))))
    assert len(training_worker._log_ship_lines) == 1
    # После восстановления API строка всё равно доезжает до журнала.
    client = _FakeClient()
    asyncio.run(training_worker.ship_logs(client))
    assert client.posts[0][1]["entries"][0]["message"] == "job 13 progress: epoch 3/40"
    assert len(training_worker._log_ship_lines) == 0


def test_updater_buffers_its_journal_and_stays_quiet_without_api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ZMK_API_URL", raising=False)
    updater = _load_module(monkeypatch, "services/updater/app.py", "zmk_updater_under_test")
    updater._log_ship_lines.clear()
    updater.ship_log("update applied: 2.16.4 -> 2.17.0, redeploy started")
    updater.ship_log("update failed: could not reach the release feed", "ERROR")
    buffered = list(updater._log_ship_lines)
    assert [entry[1] for entry in buffered] == ["INFO", "ERROR"]
    assert "redeploy started" in buffered[0][2]
    # Без ZMK_API_URL зеркало выключено: фоновая задача не создаётся.
    from fastapi.testclient import TestClient

    with TestClient(updater.app) as client:
        assert client.get("/health").status_code == 200
    updater._log_ship_lines.clear()
    sys.modules.pop("zmk_updater_under_test", None)


def test_updater_mirrors_logging_records_into_the_journal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ZMK_API_URL", raising=False)
    updater = _load_module(monkeypatch, "services/updater/app.py", "zmk_updater_logging_under_test")
    updater._log_ship_lines.clear()
    import logging

    handler = updater._ProjectLogHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logger = logging.getLogger("zmk.updater.test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.warning("docker compose redeploy exited with code 1")
    finally:
        logger.removeHandler(handler)
    levels = {entry[1] for entry in updater._log_ship_lines}
    assert "WARNING" in levels
    assert any("docker compose redeploy" in entry[2] for entry in updater._log_ship_lines)
    updater._log_ship_lines.clear()
    sys.modules.pop("zmk_updater_logging_under_test", None)


def test_log_buffer_is_bounded_so_a_silent_api_cannot_eat_memory(monkeypatch: pytest.MonkeyPatch):
    _stub_heavy_modules(monkeypatch)
    module = _load_module(monkeypatch, "services/training_worker/main.py", "zmk_training_worker_bound_test")
    for index in range(module._log_ship_lines.maxlen + 250):
        module.ship_log(f"line {index}")
    assert len(module._log_ship_lines) == module._log_ship_lines.maxlen
    assert module._log_ship_lines[-1][2] == f"line {module._log_ship_lines.maxlen + 249}"
    sys.modules.pop("zmk_training_worker_bound_test", None)


def test_updater_handler_skips_httpx_noise_but_keeps_warnings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ZMK_API_URL", raising=False)
    updater = _load_module(monkeypatch, "services/updater/app.py", "zmk_updater_noise_test")
    import logging

    handler = updater._ProjectLogHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    noisy = logging.getLogger("httpx")
    serious = logging.getLogger("zmk.updater.noise-test")
    for logger in (noisy, serious):
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    updater._log_ship_lines.clear()
    try:
        noisy.info('HTTP Request: POST http://api:8000/api/service-logs "HTTP/1.1 202 Accepted"')
        noisy.error("HTTP Request failed: connection reset by peer")
        serious.info("docker compose redeploy started")
    finally:
        for logger in (noisy, serious):
            logger.removeHandler(handler)
    messages = [entry[2] for entry in updater._log_ship_lines]
    assert not any("202 Accepted" in message for message in messages)
    assert any("connection reset by peer" in message for message in messages)
    assert any("docker compose redeploy started" in message for message in messages)
    updater._log_ship_lines.clear()
    sys.modules.pop("zmk_updater_noise_test", None)
