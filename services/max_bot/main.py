"""ZMK Vision control bot for the MAX messenger."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from maxapi import Bot, Dispatcher
from maxapi.types import Command, InputMedia, MessageCreated

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("zmk.max")
# .env remains a headless fallback; a token entered in Admin → Боты is read
# from the API's private, read-only-mounted token directory instead.
TOKEN = os.getenv("MAX_BOT_TOKEN", "")
API_URL = os.getenv("ZMK_API_URL", "http://api:8000").rstrip("/")
API_KEY = os.getenv("ZMK_API_KEY", "")


def _managed_token() -> str:
    directory=os.getenv("ZMK_BOT_TOKEN_DIR", "").strip()
    if not directory:
        return ""
    try:
        return (Path(directory) / "max.token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def bot_token() -> str:
    """Prefer the write-only Admin token and retain .env compatibility."""
    return _managed_token() or os.getenv("MAX_BOT_TOKEN", "").strip() or TOKEN.strip()


def _bot_api_token() -> str:
    path=Path(os.getenv("ZMK_BOT_API_TOKEN_FILE", "")) if os.getenv("ZMK_BOT_API_TOKEN_FILE") else (Path(os.getenv("ZMK_BOT_TOKEN_DIR", "")) / ".api-token" if os.getenv("ZMK_BOT_TOKEN_DIR") else Path("/bot-secrets/.api-token"))
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        return ""


def _ids(value: str) -> set[int]:
    result=set()
    for token in value.replace(";", ",").replace("\n", ",").split(","):
        token=token.strip()
        if token and token.lstrip("-").isdigit(): result.add(int(token))
    return result

ADMINS = _ids(os.getenv("MAX_ADMIN_IDS", ""))
OPERATORS = _ids(os.getenv("MAX_OPERATOR_IDS", ""))
VIEWERS = _ids(os.getenv("MAX_VIEWER_IDS", ""))

@dataclass
class RuntimeConfig:
    enabled: bool = False
    alerts_enabled: bool = False
    alert_min_severity: str = "high"
    admins: set[int] = field(default_factory=lambda: set(ADMINS))
    operators: set[int] = field(default_factory=lambda: set(OPERATORS))
    viewers: set[int] = field(default_factory=lambda: set(VIEWERS))
    alert_recipients: set[int] = field(default_factory=set)

RUNTIME = RuntimeConfig()
ROLE_LEVEL = {"denied": 0, "viewer": 1, "operator": 2, "admin": 3}
SEVERITY_LEVEL = {"low": 1, "medium": 2, "high": 3, "critical": 4}
bot: Bot | None = None
dp = Dispatcher()


def role_for(user_id: int) -> str:
    if user_id in RUNTIME.admins:
        return "admin"
    if user_id in RUNTIME.operators:
        return "operator"
    if user_id in RUNTIME.viewers:
        return "viewer"
    return "denied"


def allowed(user_id: int, minimum: str = "viewer") -> bool:
    return ROLE_LEVEL[role_for(user_id)] >= ROLE_LEVEL[minimum]


def alert_recipients() -> set[int]:
    return set(RUNTIME.alert_recipients) or (set(RUNTIME.admins) | set(RUNTIME.operators))


def should_alert(event: dict[str, Any]) -> bool:
    threshold=SEVERITY_LEVEL.get(RUNTIME.alert_min_severity, SEVERITY_LEVEL["high"])
    return RUNTIME.alerts_enabled and SEVERITY_LEVEL.get(str(event.get("severity", "")), 0) >= threshold


def user_id(event: MessageCreated) -> int:
    return int(getattr(event.from_user, "user_id", 0) or 0)


def args(event: MessageCreated) -> list[str]:
    return (event.message.body.text or "").strip().split()[1:]


async def guard(event: MessageCreated, minimum: str = "viewer") -> bool:
    if not RUNTIME.enabled:
        return False
    uid = user_id(event)
    if allowed(uid, minimum):
        return True
    await event.message.answer(f"⛔ Ваш MAX ID не включён в белый список.\nID: {uid}")
    return False


async def api(method: str, path: str, **kwargs: Any) -> Any:
    attempts = 3 if method.upper() == "GET" else 1
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    service_token=_bot_api_token()
    if service_token: headers["X-Bot-Service-Token"]=service_token
    async with httpx.AsyncClient(base_url=API_URL, headers=headers, timeout=15) as client:
        for attempt in range(attempts):
            try:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json() if "json" in response.headers.get("content-type", "") else response.content
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError):
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError("API request failed")


def dashboard_text(data: dict[str, Any]) -> str:
    return (
        "📊 ZMK Vision — состояние\n\n"
        f"Камеры: {data['cameras']['online']}/{data['cameras']['total']} online\n"
        f"События за 24 ч: {data['events24h']}\n"
        f"Критические: {data['critical_unacked']}\n"
        f"FPS: {data['avg_fps']}\n"
        f"Задержка: {data['avg_latency_ms']} мс\n"
        f"GPU: {str(data['gpu_load'])+'%' if data['gpu_load'] is not None else '—'}\n"
        f"Модель: {data['active_model'] or '—'}\n"
        f"Precision / Recall: {str(data['precision'])+'%' if data['precision'] is not None else '—'} / {str(data['recall'])+'%' if data['recall'] is not None else '—'}"
    )


def event_text(item: dict[str, Any]) -> str:
    labels = {"no_helmet": "Без каски", "no_vest": "Без жилета", "phone_usage": "Телефон", "smoking": "Курение", "restricted_zone": "Опасная зона", "immobility": "Неподвижность"}
    title = labels.get(item["type"], item["type"])
    return f"• {title} · {item.get('camera_name', item['camera_id'])} · {round(item['confidence'] * 100)}% · {item['severity']}"


_CAMERA_ID=re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _camera_caption(camera: dict[str, Any]) -> str:
    status="🟢 Онлайн" if camera.get("status")=="online" else "🔴 Офлайн"
    age=camera.get("snapshot_age_seconds")
    freshness="свежий кадр" if isinstance(age,(int,float)) and age<15 else f"кадр {int(age)} сек назад" if isinstance(age,(int,float)) else "кадр без времени"
    return f"📷 {camera.get('name') or camera.get('id')}\n{camera.get('zone') or 'Без зоны'} · {camera.get('id')}\n{status} · {camera.get('fps',0)} FPS · {freshness}"


async def _send_camera_snapshot(event: MessageCreated, camera_id: str) -> None:
    if not _CAMERA_ID.fullmatch(camera_id):
        await event.message.answer("Некорректный ID камеры.")
        return
    cameras=await api("GET","/api/cameras")
    camera=next((item for item in cameras if str(item.get("id"))==camera_id),None)
    if not camera:
        await event.message.answer("Камера не найдена.")
        return
    try:
        image=await api("GET",f"/api/cameras/{camera_id}/snapshot")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code==404:
            await event.message.answer(_camera_caption(camera)+"\n\n⚠️ Свежий кадр ещё не получен от inference-worker.")
            return
        raise
    if not isinstance(image,(bytes,bytearray)):
        await event.message.answer("Не удалось получить кадр камеры.")
        return
    with tempfile.NamedTemporaryFile(prefix=f"zmk-{camera_id}-",suffix=".jpg",delete=False) as handle:
        handle.write(bytes(image))
        path=Path(handle.name)
    try:
        await event.message.answer(_camera_caption(camera),attachments=[InputMedia(str(path))])
    finally:
        path.unlink(missing_ok=True)


@dp.message_created(Command("start"))
async def start(event: MessageCreated):
    if not await guard(event):
        return
    await event.message.answer(
        "ZMK Vision для MAX\n"
        f"Роль: {role_for(user_id(event))}\n\n"
        "/status /cameras /camera <camera_id> /events /health\n"
        "/logs /report /models /thresholds\n"
        "/switch_model /set_threshold /train /cancel_training /alert_test"
    )


@dp.message_created(Command("help"))
async def help_cmd(event: MessageCreated):
    await start(event)


@dp.message_created(Command("status"))
async def status(event: MessageCreated):
    if await guard(event):
        await event.message.answer(dashboard_text(await api("GET", "/api/dashboard")))


@dp.message_created(Command("cameras"))
async def cameras(event: MessageCreated):
    if not await guard(event):
        return
    data = await api("GET", "/api/cameras")
    if not data:
        await event.message.answer("📷 Камеры\n\nКамеры ещё не добавлены.")
        return
    lines=[f"{'🟢' if c.get('status') == 'online' else '🔴'} {c.get('name') or c.get('id')} · {c.get('zone') or 'Без зоны'} · {c.get('fps',0)} FPS\n  Кадр: /camera {c.get('id')}" for c in data]
    await event.message.answer("📷 Камеры\n\n" + "\n".join(lines))


@dp.message_created(Command("camera"))
async def camera_cmd(event: MessageCreated):
    if not await guard(event):
        return
    values=args(event)
    if len(values)!=1:
        await event.message.answer("Использование: /camera cam_01\nСписок ID: /cameras")
        return
    await _send_camera_snapshot(event,values[0])


@dp.message_created(Command("events"))
async def events(event: MessageCreated):
    if not await guard(event):
        return
    data = await api("GET", "/api/events?limit=10")
    await event.message.answer("🚨 Последние события\n\n" + ("\n".join(event_text(x) for x in data) or "Событий нет"))


@dp.message_created(Command("health"))
async def health(event: MessageCreated):
    if not await guard(event):
        return
    data = await api("GET", "/api/system-health")
    await event.message.answer(f"🩺 Health\nCPU {data['cpu']}% · RAM {data['ram']}% · GPU {data['gpu']}% · VRAM {data['vram']}% · Disk {data['disk']}%\nМессенджер: {data.get('messenger_provider','none')}")


@dp.message_created(Command("logs"))
async def logs_cmd(event: MessageCreated):
    if not await guard(event, "operator"):
        return
    report = await api("GET", "/api/reports/errors?hours=24")
    lines = [f"• {x['level']} {x['service']} / {x.get('camera_id') or '—'}\n  {x['message']}" for x in report["items"][:10]]
    await event.message.answer("🧾 Ошибки за 24 часа\n" + " · ".join(f"{k}: {v}" for k, v in report["summary"].items()) + "\n\n" + ("\n".join(lines) or "Ошибок нет"))


@dp.message_created(Command("report"))
async def report_cmd(event: MessageCreated):
    if not await guard(event, "operator"):
        return
    content = await api("GET", "/api/reports/events.csv")
    with tempfile.NamedTemporaryFile(prefix=f"zmk-events-{datetime.now(timezone.utc):%Y%m%d}-", suffix=".csv", delete=False) as handle:
        handle.write(content)
        path = Path(handle.name)
    try:
        await event.message.answer("📥 Отчёт по событиям", attachments=[InputMedia(str(path))])
    finally:
        path.unlink(missing_ok=True)


@dp.message_created(Command("models"))
async def models_cmd(event: MessageCreated):
    if not await guard(event):
        return
    data = await api("GET", "/api/models")
    await event.message.answer("🧠 Модели\n\n" + "\n".join(f"{'✅' if m['active'] else '▫️'} {m['name']} · P {m['precision']}% / R {m['recall']}%" for m in data))


@dp.message_created(Command("thresholds"))
async def thresholds_cmd(event: MessageCreated):
    if not await guard(event, "operator"):
        return
    values = (await api("GET", "/api/admin/config"))["inference"]
    keys = ["helmet_conf", "vest_conf", "phone_conf", "smoking_conf", "restricted_zone_conf", "immobility_conf", "nms_iou"]
    await event.message.answer("🎚 Пороги AI\n" + "\n".join(f"{key}: {values[key]}" for key in keys))


@dp.message_created(Command("switch_model"))
async def switch_model(event: MessageCreated):
    if not await guard(event, "admin"):
        return
    values = args(event)
    if len(values) != 1:
        await event.message.answer("Использование: /switch_model siz-guard-v2.1")
        return
    result = await api("POST", f"/api/models/{values[0]}/activate")
    await event.message.answer(f"✅ Hot-swap: {result['previous_model']} → {result['active_model']}\nControl-plane: {result['control_plane_switch_ms']} мс")


@dp.message_created(Command("set_threshold"))
async def set_threshold(event: MessageCreated):
    if not await guard(event, "admin"):
        return
    values = args(event)
    allowed_keys = {"helmet_conf", "vest_conf", "phone_conf", "smoking_conf", "restricted_zone_conf", "immobility_conf"}
    if len(values) != 2 or values[0] not in allowed_keys:
        await event.message.answer("Использование: /set_threshold helmet_conf 0.85")
        return
    try:
        value = float(values[1])
    except ValueError:
        await event.message.answer("Значение должно быть числом 0.1–1.0")
        return
    result = await api("PUT", f"/api/settings/{values[0]}", json={"value": value})
    await event.message.answer(f"✅ {result['key']} = {result['value']}")


@dp.message_created(Command("train"))
async def train(event: MessageCreated):
    if not await guard(event, "admin"):
        return
    values = args(event)
    if len(values) != 1:
        await event.message.answer("Использование: /train cam_01")
        return
    result = await api("POST", "/api/training/jobs", json={"camera_id": values[0], "image_count": 100, "epochs": 20})
    await event.message.answer(f"🧠 Обучение запущено: job #{result['id']}\nМодель: {result['target_name']}")


@dp.message_created(Command("cancel_training"))
async def cancel_training(event: MessageCreated):
    if not await guard(event, "admin"):
        return
    values = args(event)
    if len(values) != 1 or not values[0].isdigit():
        await event.message.answer("Использование: /cancel_training 123")
        return
    result = await api("POST", f"/api/training/jobs/{values[0]}/cancel")
    await event.message.answer(f"🛑 Задача #{result['id']} отменена")


@dp.message_created(Command("alert_test"))
async def alert_test(event: MessageCreated):
    if not await guard(event, "admin"):
        return
    await event.message.answer("✅ Канал оповещений работает. Событие или ошибка в системе не создавались.")


async def _complete_command(command_id: int, status: str, error: str = "") -> None:
    await api("POST",f"/api/bots/max/commands/{command_id}/complete",json={"status":status,"error":error[:300]})

async def _send_test_alert(text: str) -> tuple[bool, str]:
    if bot is None: return False, "MAX bot не инициализирован"
    recipients=alert_recipients()
    if not recipients: return False, "Не настроены получатели теста"
    failures=[]
    for recipient in recipients:
        try: await bot.send_message(user_id=recipient, text=f"✅ ZMK Vision\n{text}")
        except Exception as exc:  # noqa: BLE001 - messenger SDK exposes heterogeneous transport errors
            failures.append(f"{recipient}: {type(exc).__name__}")
    return (not failures, "; ".join(failures))

async def runtime_worker():
    while True:
        try:
            config=await api("GET","/api/bots/max/runtime")
            RUNTIME.enabled=bool(config.get("enabled"))
            RUNTIME.alerts_enabled=bool(config.get("alerts_enabled"))
            RUNTIME.alert_min_severity=str(config.get("alert_min_severity") or "high")
            RUNTIME.admins={int(x) for x in config.get("admin_ids",[]) }
            RUNTIME.operators={int(x) for x in config.get("operator_ids",[]) }
            RUNTIME.viewers={int(x) for x in config.get("viewer_ids",[]) }
            RUNTIME.alert_recipients={int(x) for x in config.get("alert_recipients",[]) }
            status="active" if RUNTIME.enabled else "disabled"
            detail="Управляется из Admin панели" if RUNTIME.enabled else "Отключён оператором в Admin панели"
            await api("POST","/api/bots/max/heartbeat",json={"status":status,"detail":detail,"enabled":RUNTIME.enabled})
            if RUNTIME.enabled:
                commands=await api("GET","/api/bots/max/commands")
                for command in commands.get("commands",[]):
                    if command.get("action")!="test_alert":
                        await _complete_command(int(command["id"]),"failed","Неизвестная команда")
                        continue
                    try:
                        ok,error=await _send_test_alert(str(command.get("payload",{}).get("text") or "Тестовое сообщение"))
                        await _complete_command(int(command["id"]),"completed" if ok else "failed",error)
                    except Exception as exc:
                        log.exception("MAX test alert failed")
                        await _complete_command(int(command["id"]),"failed",type(exc).__name__)
        except Exception:
            log.exception("MAX runtime configuration refresh failed")
            try: await api("POST","/api/bots/max/heartbeat",json={"status":"api_unavailable","detail":"Не удалось получить настройки Admin панели","enabled":RUNTIME.enabled})
            except Exception:
                log.debug("Could not report MAX API-unavailable heartbeat",exc_info=True)
        await asyncio.sleep(4)

async def alert_worker():
    if bot is None:
        raise RuntimeError("MAX bot is not initialized")
    last_id = 0
    while True:
        try:
            events_data = await api("GET", "/api/events?limit=50")
            if last_id == 0:
                last_id = max((x["id"] for x in events_data), default=0)
            fresh = [x for x in events_data if x["id"] > last_id and RUNTIME.enabled and should_alert(x)]
            for item in reversed(fresh):
                text = "🚨 Новое событие ZMK Vision\n" + event_text(item) + f"\nID: {item['id']}"
                for recipient in alert_recipients():
                    try:
                        await bot.send_message(user_id=recipient, text=text)
                    except Exception:
                        log.exception("MAX alert delivery failed: user_id=%s", recipient)
            last_id = max([last_id] + [x["id"] for x in events_data])
        except Exception:
            log.exception("MAX alert worker iteration failed")
        await asyncio.sleep(5)

async def _report_waiting_token() -> None:
    try:
        await api("POST","/api/bots/max/heartbeat",json={
            "status":"waiting_token",
            "detail":"Токен не задан: укажите его в Admin → Боты или MAX_BOT_TOKEN в .env",
            "enabled":False,
        })
    except Exception:
        log.debug("Could not report MAX waiting-token heartbeat",exc_info=True)


async def _wait_for_token_change(token: str) -> None:
    """Wake the polling supervisor when Admin saves or rotates a token."""
    while bot_token()==token:
        await asyncio.sleep(2)


async def run_bot_session(token: str) -> None:
    """Run one MAX polling session and restart it after token rotation."""
    global bot
    bot=Bot(token=token)
    await bot.delete_webhook()
    runtime=asyncio.create_task(runtime_worker())
    alerts=asyncio.create_task(alert_worker())
    polling=asyncio.create_task(dp.start_polling(bot))
    token_watch=asyncio.create_task(_wait_for_token_change(token))
    try:
        done,_=await asyncio.wait({polling,token_watch},return_when=asyncio.FIRST_COMPLETED)
        if token_watch in done:
            log.info("MAX token changed; reconnecting polling worker")
            # maxapi exposes an explicit stop flag; set it before cancelling
            # the in-flight long poll so a later session starts from a clean
            # dispatcher state.
            await dp.stop_polling()
        if not polling.done(): polling.cancel()
        result=await asyncio.gather(polling,return_exceptions=True)
        if token_watch not in done and isinstance(result[0],Exception):
            raise result[0]
    finally:
        for task in (runtime,alerts,token_watch):
            if not task.done(): task.cancel()
        await asyncio.gather(runtime,alerts,token_watch,return_exceptions=True)
        # maxapi's current SDK calls this ``close_session``; retain a generic
        # fallback for compatible versions without coupling deployment to one
        # exact transport implementation.
        try:
            close=getattr(bot,"close_session",None) or getattr(bot,"close",None)
            if close:
                result=close()
                if hasattr(result,"__await__"): await result
        except Exception:
            log.debug("Could not close MAX bot transport",exc_info=True)
        bot=None


async def main():
    waiting_logged=False
    while True:
        token=bot_token()
        if not token:
            if not waiting_logged:
                log.warning("MAX token is not configured; waiting for Admin → Боты")
                waiting_logged=True
            await _report_waiting_token()
            await asyncio.sleep(4)
            continue
        waiting_logged=False
        if not (ADMINS or OPERATORS or VIEWERS):
            log.warning("MAX whitelist is empty; configure roles in Admin → Боты")
        try:
            log.info("Starting MAX bot; API=%s",API_URL)
            await run_bot_session(token)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("MAX polling session stopped")
            try: await api("POST","/api/bots/max/heartbeat",json={"status":"error","detail":"Не удалось запустить MAX polling","enabled":False})
            except Exception: log.debug("Could not report MAX startup error",exc_info=True)
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
