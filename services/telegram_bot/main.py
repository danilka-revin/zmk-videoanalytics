"""ZMK Vision Telegram control plane: polling bot + Mini App launcher."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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


# --- Зеркалирование журнала в API (вкладка «Логи») ---------------------------
# Полный журнал по-прежнему доступен в `docker compose logs telegram-bot`, но
# разбирать баг удобнее в веб-консоли: строки бота дополнительно уходят в
# /api/service-logs и видны рядом с записями API и worker-ов.
LOG_SHIP_SERVICE = "bot-telegram"
LOG_SHIP_BATCH = 100
LOG_SHIP_INTERVAL_SECONDS = 5.0
_log_ship_lines: deque[tuple[str, str, str]] = deque(maxlen=400)
_log_ship_lock = threading.Lock()


def _log_ship_append(level: str, text: str) -> None:
    with _log_ship_lock:
        _log_ship_lines.append((datetime.now(timezone.utc).isoformat(timespec="seconds"), level, text[:1800]))


class _ProjectLogHandler(logging.Handler):
    """Buffer every bot log record for the unified project journal."""

    # Служебный шум ниже WARNING не шлём: иначе каждая отправка журнала
    # порождает новую строку httpx и поток зацикливается сам на себе.
    SKIP_LOGGERS = frozenset({"httpx", "httpx2", "httpcore", "asyncio", "urllib3", "aiohttp.access", "aiogram.event"})

    def emit(self, record: logging.LogRecord) -> None:
        if (record.name in self.SKIP_LOGGERS or record.name.split(".", 1)[0] in self.SKIP_LOGGERS) and record.levelno < logging.WARNING:
            return
        # Ошибку emit logging сам отдаёт в handleError: процесс бота не упадёт.
        _log_ship_append(record.levelname, self.format(record).strip())


def install_log_shipping() -> None:
    handler = _ProjectLogHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root = logging.getLogger()
    if not any(isinstance(item, _ProjectLogHandler) for item in root.handlers): root.addHandler(handler)


install_log_shipping()


async def log_ship_worker() -> None:
    """Периодически отдавать накопленные строки в единый журнал проекта."""
    while True:
        await asyncio.sleep(LOG_SHIP_INTERVAL_SECONDS)
        with _log_ship_lock:
            if not _log_ship_lines: continue
            batch = [_log_ship_lines.popleft() for _ in range(min(LOG_SHIP_BATCH, len(_log_ship_lines)))]
        payload = {"service": LOG_SHIP_SERVICE, "entries": [{"timestamp": stamp, "level": level, "message": line} for stamp, level, line in batch]}
        try:
            await api("POST", "/api/service-logs", json=payload)
        except Exception:  # сеть не должна останавливать бота
            log.debug("Project log shipping failed", exc_info=True)
            with _log_ship_lock:
                for entry in reversed(batch): _log_ship_lines.appendleft(entry)
# ``TOKEN`` is retained as the legacy .env fallback.  Admin-entered secrets
# live in a private bind mount instead, so they are never retrievable through
# the web API and can be updated while this worker is running.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_URL = os.getenv("ZMK_API_URL", "http://api:8000").rstrip("/")
WEBAPP_URL = os.getenv("TELEGRAM_WEBAPP_URL", "http://localhost:5173/telegram")
API_KEY = os.getenv("ZMK_API_KEY", "")


def _managed_token() -> str:
    directory=os.getenv("ZMK_BOT_TOKEN_DIR", "").strip()
    if not directory:
        return ""
    try:
        return (Path(directory) / "telegram.token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def bot_token() -> str:
    """Prefer the write-only Admin token and retain .env compatibility."""
    return _managed_token() or os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or TOKEN.strip()


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

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def _normalize_username(value: str | None) -> str:
    handle=str(value or "").strip().removeprefix("@")
    return "@"+handle.lower() if _USERNAME_RE.fullmatch(handle) else ""


def _role_env(role: str) -> str:
    return ",".join(item for item in (os.getenv(f"TELEGRAM_{role}_IDS", "").strip(),os.getenv(f"TELEGRAM_{role}_USERNAMES", "").strip()) if item)


def _principals(value: str) -> tuple[set[int],set[str]]:
    ids=_ids(value); usernames:set[str]=set()
    for token in value.replace(";", ",").replace("\n", ",").split(","):
        token=token.strip()
        if token and not token.lstrip("-").isdigit():
            username=_normalize_username(token)
            if username: usernames.add(username)
    return ids,usernames


ADMINS, ADMIN_USERNAMES = _principals(_role_env("ADMIN"))
OPERATORS, OPERATOR_USERNAMES = _principals(_role_env("OPERATOR"))
VIEWERS, VIEWER_USERNAMES = _principals(_role_env("VIEWER"))

@dataclass
class RuntimeConfig:
    enabled: bool = False
    alerts_enabled: bool = False
    alert_min_severity: str = "high"
    admins: set[int] = field(default_factory=lambda: set(ADMINS))
    operators: set[int] = field(default_factory=lambda: set(OPERATORS))
    viewers: set[int] = field(default_factory=lambda: set(VIEWERS))
    admin_usernames: set[str] = field(default_factory=lambda: set(ADMIN_USERNAMES))
    operator_usernames: set[str] = field(default_factory=lambda: set(OPERATOR_USERNAMES))
    viewer_usernames: set[str] = field(default_factory=lambda: set(VIEWER_USERNAMES))
    alert_recipients: set[int] = field(default_factory=set)
    webapp_url: str = ""

RUNTIME = RuntimeConfig()
router = Router()
ROLE_LEVEL = {"denied": 0, "viewer": 1, "operator": 2, "admin": 3}
SEVERITY_LEVEL = {"low": 1, "medium": 2, "high": 3, "critical": 4}

def role_for(user_id: int, username: str = "") -> str:
    handle=_normalize_username(username)
    if user_id in RUNTIME.admins or handle and handle in RUNTIME.admin_usernames: return "admin"
    if user_id in RUNTIME.operators or handle and handle in RUNTIME.operator_usernames: return "operator"
    if user_id in RUNTIME.viewers or handle and handle in RUNTIME.viewer_usernames: return "viewer"
    return "denied"
def allowed(user_id: int, minimum: str = "viewer", username: str = "") -> bool:
    return ROLE_LEVEL[role_for(user_id,username)] >= ROLE_LEVEL[minimum]

def alert_recipients() -> set[int]:
    return set(RUNTIME.alert_recipients) or (set(RUNTIME.admins) | set(RUNTIME.operators))
def should_alert(event: dict[str, Any]) -> bool:
    threshold=SEVERITY_LEVEL.get(RUNTIME.alert_min_severity, SEVERITY_LEVEL["high"])
    return RUNTIME.alerts_enabled and SEVERITY_LEVEL.get(str(event.get("severity", "")), 0) >= threshold

def menu(user_id: int, username: str = "") -> InlineKeyboardMarkup:
    rows = []
    webapp_url=RUNTIME.webapp_url or WEBAPP_URL
    if webapp_url.startswith("https://"):
        rows.append([InlineKeyboardButton(text="📊 Открыть ZMK Mini App", web_app=WebAppInfo(url=webapp_url))])
    rows += [
        [InlineKeyboardButton(text="🟢 Статус", callback_data="status"), InlineKeyboardButton(text="📷 Камеры", callback_data="cameras")],
        [InlineKeyboardButton(text="🚨 События", callback_data="events"), InlineKeyboardButton(text="🧾 Ошибки", callback_data="errors")],
        [InlineKeyboardButton(text="🧠 Модели", callback_data="models"), InlineKeyboardButton(text="🩺 Health", callback_data="health")],
    ]
    if allowed(user_id, "operator", username): rows.append([InlineKeyboardButton(text="📦 Отчёт + кадры", callback_data="report")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def api(method: str, path: str, **kwargs: Any) -> Any:
    attempts=3 if method.upper()=="GET" else 1
    headers={"X-API-Key":API_KEY} if API_KEY else {}
    service_token=_bot_api_token()
    if service_token: headers["X-Bot-Service-Token"]=service_token
    async with httpx.AsyncClient(base_url=API_URL, timeout=15, headers=headers) as client:
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
            f"GPU: <b>{str(d['gpu_load'])+'%' if d['gpu_load'] is not None else '—'}</b>\n"
            f"Precision / Recall: <b>{str(d['precision'])+'%' if d['precision'] is not None else '—'} / {str(d['recall'])+'%' if d['recall'] is not None else '—'}</b>")

def event_text(e: dict[str, Any]) -> str:
    labels={"no_helmet":"Без каски","no_vest":"Без жилета","phone_usage":"Телефон","smoking":"Курение","restricted_zone":"Опасная зона","immobility":"Неподвижность"}
    return f"• <b>{html.escape(labels.get(e['type'],e['type']))}</b> · {html.escape(str(e.get('camera_name',e['camera_id'])))} · {round(e['confidence']*100)}% · {html.escape(str(e['severity']))}"

_CAMERA_ID=re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _camera_caption(camera: dict[str, Any]) -> str:
    name=html.escape(str(camera.get("name") or camera.get("id") or "Камера"))
    zone=html.escape(str(camera.get("zone") or "Без зоны"))
    camera_id=html.escape(str(camera.get("id") or ""))
    status="🟢 Онлайн" if camera.get("status")=="online" else "🔴 Офлайн"
    age=camera.get("snapshot_age_seconds")
    freshness="свежий кадр" if isinstance(age,(int,float)) and age<15 else f"кадр {int(age)} сек назад" if isinstance(age,(int,float)) else "кадр без времени"
    return f"<b>📷 {name}</b>\n{zone} · <code>{camera_id}</code>\n{status} · {camera.get('fps',0)} FPS · {freshness}"


def _camera_keyboard(cameras: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows=[]
    for camera in cameras[:40]:
        camera_id=str(camera.get("id") or "")
        if not _CAMERA_ID.fullmatch(camera_id):
            continue
        name=str(camera.get("name") or camera_id).replace("\n"," ").strip()
        label=("🟢 " if camera.get("status")=="online" else "🔴 ")+name[:38]
        rows.append([InlineKeyboardButton(text=label,callback_data=f"camera:{camera_id}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить камеры",callback_data="cameras")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_camera_list(message: Message) -> None:
    cameras=await api("GET","/api/cameras")
    if not cameras:
        await message.answer("<b>📷 Камеры</b>\n\nКамеры ещё не добавлены.")
        return
    lines=[f"{'🟢' if c.get('status')=='online' else '🔴'} <b>{html.escape(str(c.get('name') or c.get('id')))}</b> · <code>{html.escape(str(c.get('id')))}</code> · {html.escape(str(c.get('zone') or 'Без зоны'))}" for c in cameras]
    suffix="\n\nВыберите камеру ниже — бот пришлёт последний реальный кадр." if any(_CAMERA_ID.fullmatch(str(c.get("id") or "")) for c in cameras) else ""
    await message.answer("<b>📷 Камеры</b>\n\n"+"\n".join(lines)+suffix,reply_markup=_camera_keyboard(cameras))


async def _send_camera_snapshot(message: Message, camera_id: str) -> None:
    if not _CAMERA_ID.fullmatch(camera_id):
        await message.answer("Некорректный ID камеры.")
        return
    cameras=await api("GET","/api/cameras")
    camera=next((item for item in cameras if str(item.get("id"))==camera_id),None)
    if not camera:
        await message.answer("Камера не найдена.",reply_markup=_camera_keyboard(cameras))
        return
    try:
        image=await api("GET",f"/api/cameras/{camera_id}/snapshot")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code==404:
            await message.answer(_camera_caption(camera)+"\n\n⚠️ Свежий кадр ещё не получен от inference-worker.",reply_markup=_camera_keyboard(cameras))
            return
        raise
    if not isinstance(image,(bytes,bytearray)):
        await message.answer("Не удалось получить кадр камеры.")
        return
    await message.answer_photo(BufferedInputFile(bytes(image),filename=f"zmk-{camera_id}.jpg"),caption=_camera_caption(camera),reply_markup=_camera_keyboard(cameras))


async def guard(message: Message, minimum: str = "viewer") -> bool:
    if not RUNTIME.enabled: return False
    user=message.from_user
    uid=user.id if user else 0
    username=str(user.username or "") if user else ""
    if allowed(uid, minimum, username): return True
    hint="Укажите @username в Admin → Боты." if username else "У этого аккаунта нет username — задайте username в Telegram или используйте числовой ID."
    await message.answer(f"⛔ Ваш Telegram username не включён в белый список.\n{hint}")
    return False

@router.message(Command("start"))
async def start(message: Message):
    if not await guard(message): return
    await message.answer(f"<b>ZMK Vision</b>\nРоль: <code>{role_for(message.from_user.id,message.from_user.username or '')}</code>\nУправление видеоаналитикой и отчётами.", reply_markup=menu(message.from_user.id,message.from_user.username or ''))

@router.message(Command("help"))
async def help_cmd(message: Message):
    if not await guard(message): return
    await message.answer("/status — состояние\n/cameras — список и кнопки кадров\n/camera &lt;camera_id&gt; — последний кадр\n/events — события\n/logs — ошибки\n/report — ZIP-отчёт с кадрами\n/models — модели\n/switch_model &lt;name&gt; — hot-swap (admin)\n/thresholds — пороги AI\n/set_threshold &lt;metric&gt; &lt;value&gt; — изменить порог\n/train &lt;camera_id&gt; — дообучение\n/cancel_training &lt;job_id&gt; — отменить обучение\n/users — пользователи\n/alert_test — тест тревоги\n/health — сервисы", reply_markup=menu(message.from_user.id,message.from_user.username or ''))

@router.message(Command("status"))
async def status(message: Message):
    if not await guard(message): return
    await message.answer(dashboard_text(await api("GET", "/api/dashboard")), reply_markup=menu(message.from_user.id,message.from_user.username or ''))

@router.message(Command("cameras"))
async def cameras(message: Message):
    if not await guard(message): return
    await _send_camera_list(message)

@router.message(Command("camera"))
async def camera_cmd(message: Message, command: CommandObject):
    if not await guard(message): return
    camera_id=(command.args or "").strip()
    if not camera_id:
        await message.answer("Использование: <code>/camera cam_01</code>\nИли используйте /cameras и выберите кнопку.")
        return
    await _send_camera_snapshot(message,camera_id)

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
    content=await api("GET","/api/reports/events.zip")
    await message.answer_document(BufferedInputFile(content,filename=f"zmk-events-with-evidence-{datetime.now(timezone.utc):%Y%m%d}.zip"),caption="Отчёт по событиям: русская таблица и доступные кадры нарушений")

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
    await message.answer("✅ <b>Канал оповещений работает</b>\nЭто служебная проверка доставки; событие или ошибка в системе не создавались.")

@router.message(Command("health"))
async def health_cmd(message: Message):
    if not await guard(message): return
    h=await api("GET","/api/system-health")
    await message.answer("<b>🩺 System health</b>\n\n"+f"CPU {h['cpu']}% · RAM {h['ram']}% · GPU {h['gpu']}% · VRAM {h['vram']}% · Disk {h['disk']}%\nМессенджер: {h.get('messenger_provider','none')}\n"+"\n".join(f"🟢 {html.escape(str(x['name']))}: {html.escape(str(x['status']))}" for x in h['services']))

@router.callback_query(F.data.startswith("camera:"))
async def camera_snapshot_callback(query: CallbackQuery):
    if not RUNTIME.enabled:
        await query.answer("Бот отключён оператором",show_alert=True)
        return
    if not query.from_user or not allowed(query.from_user.id,"viewer",query.from_user.username or ''):
        await query.answer("Нет доступа",show_alert=True)
        return
    camera_id=(query.data or "").removeprefix("camera:")
    if not _CAMERA_ID.fullmatch(camera_id):
        await query.answer("Некорректная камера",show_alert=True)
        return
    await query.answer("Получаю кадр…")
    if query.message:
        await _send_camera_snapshot(query.message,camera_id)


@router.callback_query(F.data.in_({"status","cameras","events","errors","models","health","report"}))
async def callbacks(query: CallbackQuery):
    if not RUNTIME.enabled: await query.answer("Бот отключён оператором",show_alert=True); return
    if not query.from_user or not allowed(query.from_user.id,"viewer",query.from_user.username or ''): await query.answer("Нет доступа",show_alert=True); return
    if query.data in {"errors","report"} and not allowed(query.from_user.id,"operator",query.from_user.username or ''): await query.answer("Нужна роль operator",show_alert=True); return
    await query.answer(); message=query.message
    if query.data=="status": await message.answer(dashboard_text(await api("GET","/api/dashboard")),reply_markup=menu(query.from_user.id,query.from_user.username or ''))
    elif query.data=="cameras":
        await _send_camera_list(message)
    elif query.data=="events":
        data=await api("GET","/api/events?limit=10"); await message.answer("<b>🚨 События</b>\n\n"+"\n".join(event_text(x) for x in data))
    elif query.data=="errors":
        r=await api("GET","/api/reports/errors?hours=24"); await message.answer("<b>🧾 Ошибки</b>\n"+" · ".join(f"{k}: <b>{v}</b>" for k,v in r['summary'].items()))
    elif query.data=="models":
        data=await api("GET","/api/models"); await message.answer("<b>🧠 Модели</b>\n\n"+"\n".join(f"{'✅' if m['active'] else '▫️'} {html.escape(str(m['name']))}" for m in data))
    elif query.data=="health":
        h=await api("GET","/api/system-health"); await message.answer(f"<b>🩺 Health</b>\nCPU {h['cpu']}% · RAM {h['ram']}% · GPU {h['gpu']}%")
    elif query.data=="report":
        content=await api("GET","/api/reports/events.zip"); await message.answer_document(BufferedInputFile(content,filename=f"zmk-events-with-evidence-{datetime.now(timezone.utc):%Y%m%d}.zip"),caption="Русская таблица и доступные кадры нарушений")

async def _complete_command(command_id: int, status: str, error: str = "") -> None:
    await api("POST",f"/api/bots/telegram/commands/{command_id}/complete",json={"status":status,"error":error[:300]})

async def _send_test_alert(bot: Bot, text: str) -> tuple[bool, str]:
    recipients=alert_recipients()
    if not recipients: return False, "Не настроены получатели теста"
    failures=[]
    for chat_id in recipients:
        try: await bot.send_message(chat_id, f"✅ <b>ZMK Vision</b>\n{text}")
        except Exception as exc:  # noqa: BLE001 - messenger SDK exposes heterogeneous transport errors
            failures.append(f"{chat_id}: {type(exc).__name__}")
    return (not failures, "; ".join(failures))

async def runtime_worker(bot: Bot):
    """Apply Admin → Bots settings without a container restart and execute safe commands."""
    while True:
        try:
            config=await api("GET","/api/bots/telegram/runtime")
            RUNTIME.enabled=bool(config.get("enabled"))
            RUNTIME.alerts_enabled=bool(config.get("alerts_enabled"))
            RUNTIME.alert_min_severity=str(config.get("alert_min_severity") or "high")
            RUNTIME.admins={int(x) for x in config.get("admin_ids",[]) }
            RUNTIME.operators={int(x) for x in config.get("operator_ids",[]) }
            RUNTIME.viewers={int(x) for x in config.get("viewer_ids",[]) }
            RUNTIME.admin_usernames={item for item in (_normalize_username(str(x)) for x in config.get("admin_usernames",[])) if item}
            RUNTIME.operator_usernames={item for item in (_normalize_username(str(x)) for x in config.get("operator_usernames",[])) if item}
            RUNTIME.viewer_usernames={item for item in (_normalize_username(str(x)) for x in config.get("viewer_usernames",[])) if item}
            RUNTIME.alert_recipients={int(x) for x in config.get("alert_recipients",[]) }
            RUNTIME.webapp_url=str(config.get("webapp_url") or "")
            status="active" if RUNTIME.enabled else "disabled"
            detail="Управляется из Admin панели" if RUNTIME.enabled else "Отключён оператором в Admin панели"
            await api("POST","/api/bots/telegram/heartbeat",json={"status":status,"detail":detail,"enabled":RUNTIME.enabled})
            if RUNTIME.enabled:
                commands=await api("GET","/api/bots/telegram/commands")
                for command in commands.get("commands",[]):
                    if command.get("action")!="test_alert":
                        await _complete_command(int(command["id"]),"failed","Неизвестная команда")
                        continue
                    try:
                        ok,error=await _send_test_alert(bot,str(command.get("payload",{}).get("text") or "Тестовое сообщение"))
                        await _complete_command(int(command["id"]),"completed" if ok else "failed",error)
                    except Exception as exc:
                        log.exception("Test alert failed")
                        await _complete_command(int(command["id"]),"failed",type(exc).__name__)
        except Exception:
            log.exception("Runtime configuration refresh failed")
            try: await api("POST","/api/bots/telegram/heartbeat",json={"status":"api_unavailable","detail":"Не удалось получить настройки Admin панели","enabled":RUNTIME.enabled})
            except Exception:
                log.debug("Could not report Telegram API-unavailable heartbeat",exc_info=True)
        await asyncio.sleep(4)

async def alert_worker(bot: Bot):
    """Push newly received configured-severity events without acknowledging them."""
    last_id=0
    while True:
        try:
            events=await api("GET","/api/events?limit=50")
            if last_id==0: last_id=max((e["id"] for e in events),default=0)
            fresh=[e for e in events if e["id"]>last_id and RUNTIME.enabled and should_alert(e)]
            for event in reversed(fresh):
                text="🚨 <b>Новое событие ZMK Vision</b>\n"+event_text(event)+f"\nID: <code>{event['id']}</code>"
                for chat_id in alert_recipients():
                    try: await bot.send_message(chat_id,text)
                    except Exception: log.exception("Alert delivery failed: chat_id=%s",chat_id)
            last_id=max([last_id]+[e["id"] for e in events])
        except Exception: log.exception("Alert worker iteration failed")
        await asyncio.sleep(5)

async def _report_waiting_token() -> None:
    try:
        await api("POST","/api/bots/telegram/heartbeat",json={
            "status":"waiting_token",
            "detail":"Токен не задан: укажите его в Admin → Боты или TELEGRAM_BOT_TOKEN в .env",
            "enabled":False,
        })
    except Exception:
        log.debug("Could not report Telegram waiting-token heartbeat",exc_info=True)


async def _wait_for_token_change(token: str) -> None:
    """Wake the polling supervisor when Admin saves or rotates a token."""
    while bot_token()==token:
        await asyncio.sleep(2)


@router.errors()
async def telegram_error(event: ErrorEvent):
    log.error("Telegram update failed",exc_info=(type(event.exception),event.exception,event.exception.__traceback__))
    message=getattr(event.update,"message",None)
    if message:
        try: await message.answer("⚠️ Команда временно недоступна. Проверьте состояние API или повторите позже.")
        except Exception:
            log.debug("Could not send user-facing error",exc_info=True)
    return True


async def run_bot_session(token: str) -> None:
    """Run one polling session, cancelling it cleanly after token rotation."""
    bot=Bot(token,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp=Dispatcher(); dp.include_router(router)
    runtime=asyncio.create_task(runtime_worker(bot))
    alerts=asyncio.create_task(alert_worker(bot))
    ship_logs=asyncio.create_task(log_ship_worker())
    polling=asyncio.create_task(dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types()))
    token_watch=asyncio.create_task(_wait_for_token_change(token))
    try:
        done,_=await asyncio.wait({polling,token_watch},return_when=asyncio.FIRST_COMPLETED)
        if token_watch in done:
            log.info("Telegram token changed; reconnecting polling worker")
        # ``start_polling`` owns a long-poll request, so cancelling the task is
        # portable across aiogram versions and releases it promptly.
        if not polling.done(): polling.cancel()
        result=await asyncio.gather(polling,return_exceptions=True)
        if token_watch not in done and isinstance(result[0],Exception):
            raise result[0]
    finally:
        for task in (runtime,alerts,token_watch,ship_logs):
            if not task.done(): task.cancel()
        await asyncio.gather(runtime,alerts,token_watch,ship_logs,return_exceptions=True)
        await bot.session.close()


async def main():
    waiting_logged=False
    while True:
        token=bot_token()
        if not token:
            if not waiting_logged:
                log.warning("Telegram token is not configured; waiting for Admin → Боты")
                waiting_logged=True
            await _report_waiting_token()
            await asyncio.sleep(4)
            continue
        waiting_logged=False
        if not (ADMINS or OPERATORS or VIEWERS): log.warning("Telegram whitelist is empty; configure roles in Admin → Боты")
        try:
            log.info("Starting Telegram bot; API=%s",API_URL)
            await run_bot_session(token)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram polling session stopped")
            try: await api("POST","/api/bots/telegram/heartbeat",json={"status":"error","detail":"Не удалось запустить Telegram polling","enabled":False})
            except Exception: log.debug("Could not report Telegram startup error",exc_info=True)
        await asyncio.sleep(1)


if __name__=="__main__": asyncio.run(main())
