"""ZMK Vision control bot for the MAX messenger."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from maxapi import Bot, Dispatcher
from maxapi.types import Command, InputMedia, MessageCreated

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("zmk.max")
TOKEN = os.getenv("MAX_BOT_TOKEN", "")
API_URL = os.getenv("ZMK_API_URL", "http://api:8000").rstrip("/")
API_KEY = os.getenv("ZMK_API_KEY", "")
ADMINS = {int(x) for x in os.getenv("MAX_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
OPERATORS = {int(x) for x in os.getenv("MAX_OPERATOR_IDS", "").split(",") if x.strip().isdigit()}
VIEWERS = {int(x) for x in os.getenv("MAX_VIEWER_IDS", "").split(",") if x.strip().isdigit()}
ROLE_LEVEL = {"denied": 0, "viewer": 1, "operator": 2, "admin": 3}
bot: Bot | None = None
dp = Dispatcher()


def role_for(user_id: int) -> str:
    if user_id in ADMINS:
        return "admin"
    if user_id in OPERATORS:
        return "operator"
    if user_id in VIEWERS:
        return "viewer"
    return "denied"


def allowed(user_id: int, minimum: str = "viewer") -> bool:
    return ROLE_LEVEL[role_for(user_id)] >= ROLE_LEVEL[minimum]


def user_id(event: MessageCreated) -> int:
    return int(getattr(event.from_user, "user_id", 0) or 0)


def args(event: MessageCreated) -> list[str]:
    return (event.message.body.text or "").strip().split()[1:]


async def guard(event: MessageCreated, minimum: str = "viewer") -> bool:
    uid = user_id(event)
    if allowed(uid, minimum):
        return True
    await event.message.answer(f"⛔ Ваш MAX ID не включён в белый список.\nID: {uid}")
    return False


async def api(method: str, path: str, **kwargs: Any) -> Any:
    attempts = 3 if method.upper() == "GET" else 1
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
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
        f"GPU: {data['gpu_load']}%\n"
        f"Модель: {data['active_model']}\n"
        f"Precision / Recall: {data['precision']}% / {data['recall']}%"
    )


def event_text(item: dict[str, Any]) -> str:
    labels = {"no_helmet": "Без каски", "no_vest": "Без жилета", "phone_usage": "Телефон", "smoking": "Курение", "restricted_zone": "Опасная зона", "immobility": "Неподвижность"}
    title = labels.get(item["type"], item["type"])
    return f"• {title} · {item.get('camera_name', item['camera_id'])} · {round(item['confidence'] * 100)}% · {item['severity']}"


@dp.message_created(Command("start"))
async def start(event: MessageCreated):
    if not await guard(event):
        return
    await event.message.answer(
        "ZMK Vision для MAX\n"
        f"Роль: {role_for(user_id(event))}\n\n"
        "/status /cameras /events /health\n"
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
    await event.message.answer("📷 Камеры\n\n" + "\n".join(f"{'🟢' if c['status'] == 'online' else '🔴'} {c['name']} · {c['zone']} · {c['fps']} FPS" for c in data))


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
    result = await api("POST", "/api/admin/logs/simulate-error")
    await event.message.answer(f"🚨 {result['level']} {result['service']}\n{result['message']}")


async def alert_worker():
    if bot is None:
        raise RuntimeError("MAX bot is not initialized")
    last_id = 0
    while True:
        try:
            events_data = await api("GET", "/api/events?limit=50")
            if last_id == 0:
                last_id = max((x["id"] for x in events_data), default=0)
            fresh = [x for x in events_data if x["id"] > last_id and x["severity"] in {"critical", "high"}]
            for item in reversed(fresh):
                text = "🚨 Новое событие ZMK Vision\n" + event_text(item) + f"\nID: {item['id']}"
                for recipient in ADMINS | OPERATORS:
                    try:
                        await bot.send_message(user_id=recipient, text=text)
                    except Exception:
                        log.exception("MAX alert delivery failed: user_id=%s", recipient)
            last_id = max([last_id] + [x["id"] for x in events_data])
        except Exception:
            log.exception("MAX alert worker iteration failed")
        await asyncio.sleep(5)


async def main():
    global bot
    if not TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN is not configured")
    bot = Bot(token=TOKEN)
    if not (ADMINS or OPERATORS or VIEWERS):
        log.warning("MAX whitelist is empty; all users will be denied")
    await bot.delete_webhook()
    alerts = asyncio.create_task(alert_worker())
    try:
        await dp.start_polling(bot)
    finally:
        alerts.cancel()
        await asyncio.gather(alerts, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
