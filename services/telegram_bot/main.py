"""ZMK Vision Telegram control plane: polling bot + Mini App launcher."""
from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("zmk.telegram")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_URL = os.getenv("ZMK_API_URL", "http://api:8000").rstrip("/")
WEBAPP_URL = os.getenv("TELEGRAM_WEBAPP_URL", "http://localhost:5173/telegram")
API_KEY = os.getenv("ZMK_API_KEY", "")
ADMINS = {int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
OPERATORS = {int(x) for x in os.getenv("TELEGRAM_OPERATOR_IDS", "").split(",") if x.strip().isdigit()}
VIEWERS = {int(x) for x in os.getenv("TELEGRAM_VIEWER_IDS", "").split(",") if x.strip().isdigit()}
router = Router()

ROLE_LEVEL = {"denied": 0, "viewer": 1, "operator": 2, "admin": 3}
def role_for(user_id: int) -> str:
    if user_id in ADMINS: return "admin"
    if user_id in OPERATORS: return "operator"
    if user_id in VIEWERS: return "viewer"
    return "denied"
def allowed(user_id: int, minimum: str = "viewer") -> bool:
    return ROLE_LEVEL[role_for(user_id)] >= ROLE_LEVEL[minimum]

def menu(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    if WEBAPP_URL.startswith("https://"):
        rows.append([InlineKeyboardButton(text="📊 Открыть ZMK Mini App", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows += [
        [InlineKeyboardButton(text="🟢 Статус", callback_data="status"), InlineKeyboardButton(text="📷 Камеры", callback_data="cameras")],
        [InlineKeyboardButton(text="🚨 События", callback_data="events"), InlineKeyboardButton(text="🧾 Ошибки", callback_data="errors")],
        [InlineKeyboardButton(text="🧠 Модели", callback_data="models"), InlineKeyboardButton(text="🩺 Health", callback_data="health")],
    ]
    if allowed(user_id, "operator"): rows.append([InlineKeyboardButton(text="📥 CSV-отчёт", callback_data="report")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def api(method: str, path: str, **kwargs: Any) -> Any:
    attempts=3 if method.upper()=="GET" else 1
    async with httpx.AsyncClient(base_url=API_URL, timeout=15, headers={"X-API-Key":API_KEY} if API_KEY else {}) as client:
        for attempt in range(attempts):
            try:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                return response.json() if "json" in content_type else response.content
            except (httpx.ConnectError,httpx.TimeoutException,httpx.RemoteProtocolError):
                if attempt+1>=attempts: raise
                await asyncio.sleep(.5*(attempt+1))
    raise RuntimeError("API request failed")

def dashboard_text(d: dict[str, Any]) -> str:
    return ("<b>📊 ZMK Vision — состояние</b>\n\n"
            f"Камеры: <b>{d['cameras']['online']}/{d['cameras']['total']}</b> online\n"
            f"События за 24 ч: <b>{d['events24h']}</b>\n"
            f"Критические: <b>{d['critical_unacked']}</b>\n"
            f"Средний FPS: <b>{d['avg_fps']}</b>\n"
            f"Задержка: <b>{d['avg_latency_ms']} мс</b>\n"
            f"GPU: <b>{d['gpu_load']}%</b>\n"
            f"Precision / Recall: <b>{d['precision']}% / {d['recall']}%</b>")

def event_text(e: dict[str, Any]) -> str:
    labels={"no_helmet":"Без каски","no_vest":"Без жилета","phone_usage":"Телефон","smoking":"Курение","restricted_zone":"Опасная зона","immobility":"Неподвижность"}
    return f"• <b>{html.escape(labels.get(e['type'],e['type']))}</b> · {html.escape(str(e.get('camera_name',e['camera_id'])))} · {round(e['confidence']*100)}% · {html.escape(str(e['severity']))}"

async def guard(message: Message, minimum: str = "viewer") -> bool:
    uid = message.from_user.id if message.from_user else 0
    if allowed(uid, minimum): return True
    await message.answer(f"⛔ Ваш Telegram ID не включён в белый список.\nID: <code>{uid}</code>")
    return False

@router.message(Command("start"))
async def start(message: Message):
    if not await guard(message): return
    await message.answer(f"<b>ZMK Vision</b>\nРоль: <code>{role_for(message.from_user.id)}</code>\nУправление видеоаналитикой и отчётами.", reply_markup=menu(message.from_user.id))

@router.message(Command("help"))
async def help_cmd(message: Message):
    if not await guard(message): return
    await message.answer("/status — состояние\n/cameras — камеры\n/events — события\n/logs — ошибки\n/report — CSV\n/models — модели\n/switch_model &lt;name&gt; — hot-swap (admin)\n/thresholds — пороги AI\n/set_threshold &lt;metric&gt; &lt;value&gt; — изменить порог\n/train &lt;camera_id&gt; — дообучение\n/cancel_training &lt;job_id&gt; — отменить обучение\n/users — пользователи\n/alert_test — тест тревоги\n/health — сервисы", reply_markup=menu(message.from_user.id))

@router.message(Command("status"))
async def status(message: Message):
    if not await guard(message): return
    await message.answer(dashboard_text(await api("GET", "/api/dashboard")), reply_markup=menu(message.from_user.id))

@router.message(Command("cameras"))
async def cameras(message: Message):
    if not await guard(message): return
    data=await api("GET","/api/cameras")
    text="<b>📷 Камеры</b>\n\n"+"\n".join(f"{'🟢' if c['status']=='online' else '🔴'} <b>{html.escape(str(c['name']))}</b> · {html.escape(str(c['zone']))} · {c['fps']} FPS" for c in data)
    await message.answer(text)

@router.message(Command("events"))
async def events(message: Message):
    if not await guard(message): return
    data=await api("GET","/api/events?limit=10")
    await message.answer("<b>🚨 Последние события</b>\n\n"+("\n".join(event_text(x) for x in data) or "Событий нет"))

@router.message(Command("logs"))
async def logs_cmd(message: Message):
    if not await guard(message, "operator"): return
    report=await api("GET","/api/reports/errors?hours=24")
    lines=[f"• <code>{html.escape(str(x['level']))}</code> {html.escape(str(x['service']))} / {html.escape(str(x.get('camera_id') or '—'))}\n  {html.escape(str(x['message']))}" for x in report['items'][:10]]
    await message.answer("<b>🧾 Ошибки за 24 часа</b>\n"+" · ".join(f"{k}: <b>{v}</b>" for k,v in report['summary'].items())+"\n\n"+("\n".join(lines) or "Ошибок нет"))

@router.message(Command("report"))
async def report_cmd(message: Message):
    if not await guard(message, "operator"): return
    content=await api("GET","/api/reports/events.csv")
    await message.answer_document(BufferedInputFile(content,filename=f"zmk-events-{datetime.now(timezone.utc):%Y%m%d}.csv"),caption="Отчёт по событиям")

@router.message(Command("models"))
async def models_cmd(message: Message):
    if not await guard(message): return
    data=await api("GET","/api/models")
    text="<b>🧠 Реестр моделей</b>\n\n"+"\n".join(f"{'✅' if m['active'] else '▫️'} <b>{html.escape(str(m['name']))}</b> · P {m['precision']}% / R {m['recall']}%" for m in data)
    await message.answer(text)

@router.message(Command("switch_model"))
async def switch_model(message: Message, command: CommandObject):
    if not await guard(message,"admin"): return
    if not command.args: await message.answer("Использование: <code>/switch_model siz-guard-v2.1</code>"); return
    result=await api("POST",f"/api/models/{command.args.strip()}/activate")
    await message.answer(f"✅ Hot-swap выполнен\n<code>{result['previous_model']}</code> → <code>{result['active_model']}</code>\nПростой: {result['downtime_ms']} мс")

@router.message(Command("thresholds"))
async def thresholds_cmd(message: Message):
    if not await guard(message,"operator"): return
    cfg=await api("GET","/api/admin/config"); ai=cfg["inference"]
    await message.answer("<b>🎚 Пороги AI</b>\n"+"\n".join(f"<code>{k}</code>: <b>{ai[k]}</b>" for k in ["helmet_conf","vest_conf","phone_conf","smoking_conf","restricted_zone_conf","immobility_conf","nms_iou"]))

@router.message(Command("set_threshold"))
async def set_threshold_cmd(message: Message, command: CommandObject):
    if not await guard(message,"admin"): return
    parts=(command.args or "").split()
    if len(parts)!=2 or parts[0] not in {"helmet_conf","vest_conf","phone_conf","smoking_conf","restricted_zone_conf","immobility_conf"}:
        await message.answer("Использование: <code>/set_threshold helmet_conf 0.85</code>"); return
    try: value=float(parts[1])
    except ValueError: await message.answer("Значение должно быть числом 0.1–1.0"); return
    await api("PUT",f"/api/settings/{parts[0]}",json={"value":value})
    await message.answer(f"✅ <code>{parts[0]}</code> = <b>{value}</b>")

@router.message(Command("train"))
async def train_cmd(message: Message, command: CommandObject):
    if not await guard(message,"admin"): return
    camera=(command.args or "").strip()
    if not camera: await message.answer("Использование: <code>/train cam_01</code>"); return
    job=await api("POST","/api/training/jobs",json={"camera_id":camera,"image_count":100,"epochs":20})
    await message.answer(f"🧠 Дообучение запущено\nJob: <b>#{job['id']}</b>\nМодель: <code>{html.escape(str(job['target_name']))}</code>")

@router.message(Command("cancel_training"))
async def cancel_training_cmd(message: Message, command: CommandObject):
    if not await guard(message,"admin"): return
    value=(command.args or "").strip()
    if not value.isdigit(): await message.answer("Использование: <code>/cancel_training 123</code>"); return
    result=await api("POST",f"/api/training/jobs/{value}/cancel")
    await message.answer(f"🛑 Задача <b>#{result['id']}</b> отменена")

@router.message(Command("users"))
async def users_cmd(message: Message):
    if not await guard(message,"admin"): return
    users=await api("GET","/api/admin/users")
    await message.answer("<b>👥 Пользователи</b>\n\n"+"\n".join(f"{'🟢' if u['active'] else '⚪'} {html.escape(str(u['name']))} · <code>{html.escape(str(u['role']))}</code>" for u in users))

@router.message(Command("alert_test"))
async def alert_test_cmd(message: Message):
    if not await guard(message,"admin"): return
    error=await api("POST","/api/admin/logs/simulate-error")
    await message.answer(f"🚨 <b>Тестовое оповещение</b>\n<code>{html.escape(str(error['level']))}</code> {html.escape(str(error['service']))}\n{html.escape(str(error['message']))}")

@router.message(Command("health"))
async def health_cmd(message: Message):
    if not await guard(message): return
    h=await api("GET","/api/system-health")
    await message.answer("<b>🩺 System health</b>\n\n"+f"CPU {h['cpu']}% · RAM {h['ram']}% · GPU {h['gpu']}% · VRAM {h['vram']}% · Disk {h['disk']}%\nМессенджер: {h.get('messenger_provider','none')}\n"+"\n".join(f"🟢 {html.escape(str(x['name']))}: {html.escape(str(x['status']))}" for x in h['services']))

@router.callback_query(F.data.in_({"status","cameras","events","errors","models","health","report"}))
async def callbacks(query: CallbackQuery):
    if not query.from_user or not allowed(query.from_user.id): await query.answer("Нет доступа",show_alert=True); return
    if query.data in {"errors","report"} and not allowed(query.from_user.id,"operator"): await query.answer("Нужна роль operator",show_alert=True); return
    await query.answer(); message=query.message
    if query.data=="status": await message.answer(dashboard_text(await api("GET","/api/dashboard")),reply_markup=menu(query.from_user.id))
    elif query.data=="cameras":
        data=await api("GET","/api/cameras"); await message.answer("<b>📷 Камеры</b>\n\n"+"\n".join(f"{'🟢' if c['status']=='online' else '🔴'} <b>{html.escape(str(c['name']))}</b> · {c['fps']} FPS" for c in data))
    elif query.data=="events":
        data=await api("GET","/api/events?limit=10"); await message.answer("<b>🚨 События</b>\n\n"+"\n".join(event_text(x) for x in data))
    elif query.data=="errors":
        r=await api("GET","/api/reports/errors?hours=24"); await message.answer("<b>🧾 Ошибки</b>\n"+" · ".join(f"{k}: <b>{v}</b>" for k,v in r['summary'].items()))
    elif query.data=="models":
        data=await api("GET","/api/models"); await message.answer("<b>🧠 Модели</b>\n\n"+"\n".join(f"{'✅' if m['active'] else '▫️'} {html.escape(str(m['name']))}" for m in data))
    elif query.data=="health":
        h=await api("GET","/api/system-health"); await message.answer(f"<b>🩺 Health</b>\nCPU {h['cpu']}% · RAM {h['ram']}% · GPU {h['gpu']}%")
    elif query.data=="report":
        content=await api("GET","/api/reports/events.csv"); await message.answer_document(BufferedInputFile(content,filename=f"zmk-events-{datetime.now(timezone.utc):%Y%m%d}.csv"))

async def alert_worker(bot: Bot):
    """Push newly received critical events to operators without acknowledging them."""
    last_id=0
    while True:
        try:
            events=await api("GET","/api/events?limit=50")
            if last_id==0: last_id=max((e["id"] for e in events),default=0)
            fresh=[e for e in events if e["id"]>last_id and e["severity"] in {"critical","high"}]
            for event in reversed(fresh):
                text="🚨 <b>Новое событие ZMK Vision</b>\n"+event_text(event)+f"\nID: <code>{event['id']}</code>"
                for chat_id in ADMINS|OPERATORS:
                    try: await bot.send_message(chat_id,text)
                    except Exception: log.exception("Alert delivery failed: chat_id=%s",chat_id)
            last_id=max([last_id]+[e["id"] for e in events])
        except Exception: log.exception("Alert worker iteration failed")
        await asyncio.sleep(5)

@router.errors()
async def telegram_error(event: ErrorEvent):
    log.error("Telegram update failed",exc_info=(type(event.exception),event.exception,event.exception.__traceback__))
    message=getattr(event.update,"message",None)
    if message:
        try: await message.answer("⚠️ Команда временно недоступна. Проверьте состояние API или повторите позже.")
        except Exception:
            log.debug("Could not send user-facing error",exc_info=True)
    return True

async def main():
    if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    if not (WEBAPP_URL.startswith(("https://","http://localhost"))): raise RuntimeError("TELEGRAM_WEBAPP_URL must use HTTPS")
    if not (ADMINS or OPERATORS or VIEWERS): log.warning("Telegram whitelist is empty; all users will be denied")
    bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp=Dispatcher(); dp.include_router(router)
    log.info("Starting bot; API=%s; admins=%d operators=%d viewers=%d",API_URL,len(ADMINS),len(OPERATORS),len(VIEWERS))
    alerts=asyncio.create_task(alert_worker(bot))
    try: await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally: alerts.cancel()

if __name__=="__main__": asyncio.run(main())
