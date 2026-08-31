from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import smtplib
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.message import EmailMessage
from html import escape as html_escape
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

import httpx
import psutil
import pynvml
import yaml
from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

APP_VERSION = "2.16.2"
TZ = timezone(timedelta(hours=7))
CAMERA_TELEMETRY_STALE_SECONDS = 30
HIGH_FPS_MODE = os.getenv("CAMERA_HIGH_FPS_MODE", "true").strip().lower() not in {"0","false","no","off"}
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "")) if os.getenv("SNAPSHOT_DIR") else None
# Event evidence is persisted beside the SQLite database by default. It is
# populated only by the internal inference worker after a real event is accepted.
EVENT_FRAME_DIR = Path(os.getenv("EVENT_FRAME_DIR", "")) if os.getenv("EVENT_FRAME_DIR") else None
DB_PATH = Path(os.getenv("VIDEOANALYTICS_DB", str(Path(__file__).resolve().parent.parent / "videoanalytics.db")))
STARTED = time.time()
API_KEY = os.getenv("ZMK_API_KEY", "").strip()
PASSWORD_AUTH_ENABLED=os.getenv("ZMK_PASSWORD_AUTH","false").strip().lower() not in {"0","false","no","off"}
DEFAULT_INITIAL_APP_PASSWORD="1234"  # nosec B105 - operator-visible bootstrap default, changed on first login
LEGACY_INITIAL_APP_PASSWORD="1243"  # nosec B105 - legacy bootstrap default, corrected on upgrade
AUTH_INITIAL_PASSWORD_VERSION="2"  # nosec B105 - version marker only, not a credential

def _resolve_initial_app_password(value: str | None) -> str:
    """Keep the first-login default stable across older copied .env files."""
    # 1243 was published as the first password in the preceding release. Treat
    # it as the corrected default rather than preserving the typo indefinitely.
    if value in {None,"",LEGACY_INITIAL_APP_PASSWORD}:
        return DEFAULT_INITIAL_APP_PASSWORD
    return value

INITIAL_APP_PASSWORD=_resolve_initial_app_password(os.getenv("ZMK_INITIAL_PASSWORD"))
AUTH_COOKIE_NAME="zmk_session"
try: AUTH_SESSION_HOURS=max(1,min(720,int(os.getenv("ZMK_AUTH_SESSION_HOURS","12") or 12)))
except ValueError: AUTH_SESSION_HOURS=12
AUTH_COOKIE_SECURE=os.getenv("ZMK_AUTH_COOKIE_SECURE","false").strip().lower() in {"1","true","yes","on"}
try: AUTH_RECOVERY_MINUTES=max(5,min(60,int(os.getenv("ZMK_RECOVERY_MINUTES","15") or 15)))
except ValueError: AUTH_RECOVERY_MINUTES=15
SMTP_HOST=os.getenv("SMTP_HOST","").strip()
try: SMTP_PORT=max(1,min(65535,int(os.getenv("SMTP_PORT","587") or 587)))
except ValueError: SMTP_PORT=587
SMTP_USERNAME=os.getenv("SMTP_USERNAME","").strip()
SMTP_PASSWORD=os.getenv("SMTP_PASSWORD","")
SMTP_FROM=os.getenv("SMTP_FROM","").strip()
SMTP_USE_TLS=os.getenv("SMTP_USE_TLS","true").strip().lower() not in {"0","false","no","off"}
# Port 465 uses implicit TLS; port 587 normally uses STARTTLS. SSL takes
# precedence when both toggles are accidentally enabled.
SMTP_USE_SSL=os.getenv("SMTP_USE_SSL","false").strip().lower() in {"1","true","yes","on"}
_auth_attempts: dict[str,list[float]] = {}
try: RATE_LIMIT_PER_MINUTE = max(10,int(os.getenv("RATE_LIMIT_PER_MINUTE", "120")))
except ValueError: RATE_LIMIT_PER_MINUTE = 120
_rate_buckets: dict[str, list[float]] = {}
_training_tasks: dict[int,asyncio.Task] = {}
_dataset_capture_tasks: dict[int,asyncio.Task] = {}
# Latest live JPEG per camera. Persistent snapshots remain on disk; this
# in-memory cache exists solely for the low-latency MJPEG browser stream.
_live_frames: dict[str, tuple[int, float, bytes]] = {}
_live_frames_lock = threading.Lock()
_live_frame_sequence = 0
# Live visual boxes for overlay: camera_id -> (timestamp, visual_dict)
# Visual dict contains boxes with bbox/label/semantic/confidence, not baked into JPEG
# This allows true VLC-like raw preview at full FPS with lightweight overlay on top
_live_visuals: dict[str, tuple[float, dict[str, Any]]] = {}
_live_visuals_lock = threading.Lock()
MESSENGER_PROVIDER = os.getenv("MESSENGER_PROVIDER", "none").lower()
if MESSENGER_PROVIDER not in {"none", "telegram", "max"}: MESSENGER_PROVIDER = "none"
# go2rtc is the preferred WebRTC transport for the browser camera preview. The
# API mirrors the camera RTSP list into go2rtc so the panel can play raw,
# near-live video without repainting full MJPEG frames. Keep the MJPEG
# endpoints as a fallback for installations that disable or lose go2rtc.
GO2RTC_API_URL = os.getenv("GO2RTC_API_URL", "").rstrip("/")
GO2RTC_RTSP_URL = os.getenv("GO2RTC_RTSP_URL", "rtsp://host.docker.internal:8554").rstrip("/")
GO2RTC_ENABLED = os.getenv("GO2RTC_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
GO2RTC_RTSP_TRANSPORT = os.getenv("GO2RTC_RTSP_TRANSPORT", "tcp").strip().lower()
if GO2RTC_RTSP_TRANSPORT not in {"tcp", "udp"}:
    GO2RTC_RTSP_TRANSPORT = "tcp"
GO2RTC_SYNC_TIMEOUT_SECONDS = 5.0
GO2RTC_USE_FOR_INFERENCE = os.getenv("GO2RTC_USE_FOR_INFERENCE", "true").strip().lower() not in {"0", "false", "no", "off"}
TRAINING_WORKER_URL = os.getenv("TRAINING_WORKER_URL", "").rstrip("/")
DATASET_DIR = Path(os.getenv("DATASET_DIR", "")) if os.getenv("DATASET_DIR") else (DB_PATH.parent / "datasets")
MODEL_DIR = Path(os.getenv("MODEL_DIR", "")) if os.getenv("MODEL_DIR") else (DB_PATH.parent / "models")
# Uploads are streamed to the shared model volume instead of being accumulated
# in API memory. Keep an explicit, operator-configurable ceiling because model
# artifacts can legitimately be hundreds of megabytes.
try: MODEL_UPLOAD_MAX_BYTES=max(1_000_000,min(2_000_000_000,int(os.getenv("MODEL_UPLOAD_MAX_BYTES","2_000_000_000"))))
except ValueError: MODEL_UPLOAD_MAX_BYTES=2_000_000_000
try: MODEL_TEST_CONF_DEFAULT=max(.01,min(.95,float(os.getenv("MODEL_TEST_CONF",".10") or .10)))
except ValueError: MODEL_TEST_CONF_DEFAULT=.10
WORKER_TOKEN_FILE = Path(os.getenv("ZMK_WORKER_TOKEN_FILE", "")) if os.getenv("ZMK_WORKER_TOKEN_FILE") else (MODEL_DIR / ".worker-token")

def provision_worker_token(token_file: Path, env_token: str | None = None) -> str:
    """Return the worker token, auto-provisioning a strong one if unset.

    Precedence:
      1) an explicit env token (operator-specified)
      2) a shared secret file on the model-data volume (default
         token_file = /models/.worker-token), generated once and read
         identically by the api and all workers so they always agree.

    This makes `docker compose --profile inference up` work out of the box
    without manually setting ZMK_WORKER_TOKEN, while keeping a real
    cryptographically-random shared secret. The internal endpoints are not
    published to the host network in docker-compose.yml.
    """
    tok = (env_token or "").strip()
    if tok:
        return tok
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        if token_file.is_file():
            existing = token_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        generated = secrets.token_hex(32)
        tmp = token_file.with_suffix(".tmp")
        tmp.write_text(generated, encoding="utf-8")
        tmp.replace(token_file)
        token_file.chmod(0o600)
        return generated
    except OSError:
        return ""

WORKER_TOKEN = provision_worker_token(WORKER_TOKEN_FILE, os.getenv("ZMK_WORKER_TOKEN", ""))
_bot_token_dir=os.getenv("ZMK_BOT_TOKEN_DIR","").strip()
BOT_API_TOKEN_FILE=Path(os.getenv("ZMK_BOT_API_TOKEN_FILE", "")) if os.getenv("ZMK_BOT_API_TOKEN_FILE") else ((Path(_bot_token_dir)/".api-token") if _bot_token_dir else (DB_PATH.parent/".bot-api-token"))
BOT_API_TOKEN=provision_worker_token(BOT_API_TOKEN_FILE,os.getenv("ZMK_BOT_API_TOKEN", ""))
UPDATE_SERVICE_URL = os.getenv("UPDATE_SERVICE_URL", "").rstrip("/")
# Catalog of ready-to-download pretrained models.  Generic COCO weights are
# useful for fine-tuning, but COCO does not contain a safety-helmet class.  The
# PPE entry below is an opt-in public YOLO11 baseline with person, helmet and
# no-helmet labels.  It is deliberately marked as a *trial* model: it can be
# explicitly enabled for an on-site check, but it never pretends to have local
# validation metrics.
MODEL_PRESETS = [
    {
        "id":"yolo11n","name":"yolo11n","format":"PyTorch","url":"https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt",
        "size_bytes":5613764,"classes":80,"category":"starter","description":"Компактная базовая COCO-модель (5.4 МБ) — быстрый старт для дообучения на своих данных.",
        "hint":"COCO знает класс person, но не знает защитную каску. Метрики не заданы — для активации обучите на своих данных или укажите метрики валидации.",
    },
    {
        "id":"ppe-person-helmet-yolo11","name":"ppe-person-helmet-yolo11","format":"PyTorch",
        # Pin the immutable Hugging Face revision containing the weights rather
        # than downloading an arbitrary future revision from the `main` branch.
        "url":"https://huggingface.co/melihuzunoglu/ppe-detection/resolve/4a4d54e425f82896f1717637603ec28553d7f91c/best.pt?download=true",
        "source_url":"https://huggingface.co/melihuzunoglu/ppe-detection","size_bytes":5_750_000,"min_bytes":1_000_000,"classes":4,"category":"safety","trial_activation":True,
        "description":"Готовая YOLO11 PPE-модель: человек, каска, без каски и жилет. Нарушение «без каски» привязывается к человеку.",
        "hint":"Сторонняя стартовая модель для теста на ваших камерах. Перед промышленным использованием проверьте её на местных кадрах и дообучите на своём датасете.",
    },
    {
        "id":"yolov8n","name":"yolov8n","format":"PyTorch","url":"https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt",
        "size_bytes":6549796,"classes":80,"category":"base","description":"Базовая YOLOv8-nano (6.5 МБ) — классический старт для детекции объектов.",
        "hint":"COCO знает класс person, но не знает защитную каску. Метрики не заданы — требуется валидация перед активацией.",
    },
    {
        "id":"yolo11s","name":"yolo11s","format":"PyTorch","url":"https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s.pt",
        "size_bytes":19313732,"classes":80,"category":"quality","description":"Более точная COCO-модель (19 МБ) — выше качество детекции, больше ресурсов.",
        "hint":"COCO знает класс person, но не знает защитную каску. Метрики не заданы — требуется валидация перед активацией.",
    },
]
# Allow overriding/extending the catalog via env (JSON array), for self-hosted
# model sources.
_ZMK = os.getenv("ZMK_MODEL_PRESETS_JSON", "").strip()
try:
    if _ZMK:
        MODEL_PRESETS = json.loads(_ZMK)
except (ValueError, TypeError):
    pass
# Each role is executed as an independent model in the inference pipeline.
# A general model stays available as the compatibility/primary model, while
# specialised slots let an operator combine best-of-breed detectors.
MODEL_PIPELINE_ROLES={
    "people":"Люди",
    "helmet":"Каски",
    "workwear":"Спецодежда / жилеты",
    "phone":"Телефоны",
    "smoking":"Курение",
    "zone":"Опасные зоны",
}
UPDATE_TOKEN = os.getenv("ZMK_UPDATE_TOKEN", "").strip()
SEED_TEST_DATA = os.getenv("ZMK_SEED_TEST_DATA", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ROLES = {**{int(x):"viewer" for x in os.getenv("TELEGRAM_VIEWER_IDS","").split(",") if x.strip().isdigit()},**{int(x):"operator" for x in os.getenv("TELEGRAM_OPERATOR_IDS","").split(",") if x.strip().isdigit()},**{int(x):"admin" for x in os.getenv("TELEGRAM_ADMIN_IDS","").split(",") if x.strip().isdigit()}}

def now_iso(): return datetime.now(TZ).isoformat(timespec="seconds")
def timestamp_age_seconds(value: str | None) -> int | None:
    if not value: return None
    try:
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=TZ)
        return max(0,int((datetime.now(TZ)-stamp.astimezone(TZ)).total_seconds()))
    except (TypeError,ValueError):
        return None
def db():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    con = sqlite3.connect(DB_PATH,timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con

def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt=salt or secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),salt,260_000)
    return "pbkdf2_sha256$260000$"+base64.urlsafe_b64encode(salt).decode()+"$"+base64.urlsafe_b64encode(digest).decode()


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm,rounds,salt_b64,digest_b64=encoded.split("$",3)
        if algorithm!="pbkdf2_sha256": return False
        salt=base64.urlsafe_b64decode(salt_b64.encode())
        expected=base64.urlsafe_b64decode(digest_b64.encode())
        actual=hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),salt,int(rounds))
        return hmac.compare_digest(actual,expected)
    except (TypeError,ValueError,binascii.Error):
        return False


def _initialize_or_upgrade_auth_password(con: sqlite3.Connection) -> None:
    """Create the initial password once and correct the previous 1243 default.

    The hash is normally immutable until the account owner changes or resets
    it.  The sole migration path is an unversioned database that still has the
    prior public setup password and is marked as requiring its first change.
    This lets an upgrade fix an older copied `.env` without touching a password
    the owner has already chosen.
    """
    password_row=con.execute("SELECT value FROM settings WHERE key='auth_password_hash'").fetchone()
    if not password_row or not str(password_row[0]).strip():
        con.execute("INSERT INTO settings(key,value) VALUES('auth_password_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(_hash_password(INITIAL_APP_PASSWORD),))
        con.execute("INSERT INTO settings(key,value) VALUES('auth_initial_password_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(AUTH_INITIAL_PASSWORD_VERSION,))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","auth","Initial local password initialized; change it after first login"))
        return

    version_row=con.execute("SELECT value FROM settings WHERE key='auth_initial_password_version'").fetchone()
    if version_row and str(version_row[0]).strip():
        return
    must_change_row=con.execute("SELECT value FROM settings WHERE key='auth_password_must_change'").fetchone()
    must_change=bool(must_change_row and str(must_change_row[0]).strip().lower()=="true")
    if must_change and _password_matches(LEGACY_INITIAL_APP_PASSWORD,str(password_row[0])):
        con.execute("UPDATE settings SET value=? WHERE key='auth_password_hash'",(_hash_password(INITIAL_APP_PASSWORD),))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","auth","Legacy initial password corrected; change it after first login"))
    con.execute("INSERT INTO settings(key,value) VALUES('auth_initial_password_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(AUTH_INITIAL_PASSWORD_VERSION,))


def _auth_setting(key: str, default: str = "") -> str:
    con=db()
    try:
        row=con.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
        return str(row[0]) if row else default
    finally:
        con.close()


def _smtp_ready() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def _masked_email(value: str) -> str:
    if "@" not in value: return ""
    local,domain=value.split("@",1)
    return (local[:1]+"***@"+domain) if local else "***@"+domain


def _auth_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _auth_session(request: Request) -> dict[str,Any] | None:
    token=request.cookies.get(AUTH_COOKIE_NAME,"")
    if not token or len(token)>512:
        return None
    con=db()
    try:
        row=con.execute("SELECT id,expires_at,last_seen_at FROM auth_sessions WHERE token_hash=?",(_auth_token_digest(token),)).fetchone()
        if not row:
            return None
        if str(row[1])<=now_iso():
            con.execute("DELETE FROM auth_sessions WHERE id=?",(row[0],)); con.commit()
            return None
        last_seen=str(row[2] or "")
        # Keep the active-session view useful without writing SQLite for every
        # read-only dashboard poll.
        if timestamp_age_seconds(last_seen) is None or timestamp_age_seconds(last_seen)>300:
            last_seen=now_iso(); con.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id=?",(last_seen,row[0])); con.commit()
        return {"id":str(row[0]),"expires_at":str(row[1]),"last_seen_at":last_seen}
    finally:
        con.close()


def _create_auth_session() -> tuple[str,str]:
    token=secrets.token_urlsafe(36)
    session_id=uuid.uuid4().hex
    expires=(datetime.now(TZ)+timedelta(hours=AUTH_SESSION_HOURS)).isoformat(timespec="seconds")
    con=db()
    try:
        con.execute("DELETE FROM auth_sessions WHERE expires_at<=?",(now_iso(),))
        con.execute("INSERT INTO auth_sessions(id,token_hash,created_at,expires_at,last_seen_at) VALUES(?,?,?,?,?)",(session_id,_auth_token_digest(token),now_iso(),expires,now_iso()))
        con.commit()
    finally:
        con.close()
    return token,expires


def _revoke_auth_sessions(except_id: str = "") -> None:
    con=db()
    try:
        if except_id: con.execute("DELETE FROM auth_sessions WHERE id!=?",(except_id,))
        else: con.execute("DELETE FROM auth_sessions")
        con.commit()
    finally:
        con.close()


def _set_auth_cookie(response: Response, token: str, request: Request) -> None:
    secure=AUTH_COOKIE_SECURE or request.headers.get("X-Forwarded-Proto","").lower()=="https"
    response.set_cookie(AUTH_COOKIE_NAME,token,max_age=AUTH_SESSION_HOURS*3600,httponly=True,secure=secure,samesite="lax",path="/")


def _allow_auth_attempt(request: Request) -> bool:
    now=time.time()
    source=request.headers.get("X-Real-IP") or (request.client.host if request.client else "unknown")
    # A hostile stream of spoofed source labels must not grow the in-memory
    # login limiter forever. Keep the same ten-minute policy while bounding it.
    if len(_auth_attempts)>=10_000 and source not in _auth_attempts:
        for key,values in list(_auth_attempts.items()):
            kept=[value for value in values if now-value<600]
            if kept: _auth_attempts[key]=kept
            else: _auth_attempts.pop(key,None)
        if len(_auth_attempts)>=10_000:
            return False
    attempts=[value for value in _auth_attempts.get(source,[]) if now-value<600]
    _auth_attempts[source]=attempts
    if len(attempts)>=5:
        return False
    attempts.append(now)
    _auth_attempts[source]=attempts
    return True


def _send_recovery_email(address: str, code: str) -> None:
    if not _smtp_ready():
        raise RuntimeError("SMTP не настроен")
    message=EmailMessage()
    message["Subject"]="ZMK Vision — код восстановления пароля"
    message["From"]=SMTP_FROM
    message["To"]=address
    message.set_content(f"Код восстановления ZMK Vision: {code}\n\nОн действует {AUTH_RECOVERY_MINUTES} минут. Если вы не запрашивали восстановление, проигнорируйте это письмо.")
    if SMTP_USE_SSL:
        client=smtplib.SMTP_SSL(SMTP_HOST,SMTP_PORT,timeout=20,context=ssl.create_default_context())
    else:
        client=smtplib.SMTP(SMTP_HOST,SMTP_PORT,timeout=20)
    with client:
        if SMTP_USE_TLS and not SMTP_USE_SSL:
            client.starttls(context=ssl.create_default_context())
        if SMTP_USERNAME:
            client.login(SMTP_USERNAME,SMTP_PASSWORD)
        client.send_message(message)


def event_frame_path_for(event_id:int) -> Path:
    """Return the single safe evidence JPEG location for an event."""
    if event_id < 1:
        raise HTTPException(404,"Событие не найдено")
    base=EVENT_FRAME_DIR or (DB_PATH.parent/"event_frames")
    target=(base/f"{event_id}.jpg").resolve()
    if target.parent != base.resolve():
        raise HTTPException(400,"Недопустимый путь кадра события")
    return target

def remove_event_frames(event_ids:list[int]) -> None:
    for event_id in event_ids:
        try: event_frame_path_for(int(event_id)).unlink(missing_ok=True)
        except (OSError,ValueError): pass

def apply_retention(con:sqlite3.Connection|None=None):
    own=con is None; connection=con or db()
    row=connection.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()
    if row:
        try: days=max(1,min(3650,int(float(row[0]))))
        except ValueError: days=90
        cutoff=(datetime.now(TZ)-timedelta(days=days)).isoformat()
        expired_ids=[int(row[0]) for row in connection.execute("SELECT id FROM events WHERE timestamp<?",(cutoff,)).fetchall()]
        connection.execute("DELETE FROM events WHERE timestamp<?",(cutoff,))
        remove_event_frames(expired_ids)
        connection.execute("DELETE FROM logs WHERE timestamp<?",(cutoff,))
    if own: connection.commit(); connection.close()

def init_db():
    con = db()
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY, name TEXT NOT NULL, zone TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', rtsp_url TEXT NOT NULL DEFAULT '', fps_limit REAL NOT NULL DEFAULT 8, status TEXT NOT NULL DEFAULT 'unknown', fps REAL NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, telemetry_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '', restart_requested_at TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, camera_id TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, confidence REAL NOT NULL, person_id TEXT, external_id TEXT, acknowledged INTEGER NOT NULL DEFAULT 0, review_status TEXT NOT NULL DEFAULT 'pending', reviewed_at TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', FOREIGN KEY(camera_id) REFERENCES cameras(id));
    CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, level TEXT NOT NULL, service TEXT NOT NULL, message TEXT NOT NULL, camera_id TEXT);
    CREATE TABLE IF NOT EXISTS worker_status(name TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', camera_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, model_name TEXT NOT NULL DEFAULT '', model_status TEXT NOT NULL DEFAULT 'none', model_error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS model_registry(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, format TEXT NOT NULL, status TEXT NOT NULL, precision REAL, recall REAL, trained_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'external', artifact_uri TEXT NOT NULL DEFAULT '', checksum TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS training_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, camera_id TEXT NOT NULL, base_model TEXT NOT NULL, target_name TEXT NOT NULL, image_count INTEGER NOT NULL, epochs INTEGER NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL, error TEXT, batch INTEGER NOT NULL DEFAULT 8, imgsz INTEGER NOT NULL DEFAULT 640, patience INTEGER NOT NULL DEFAULT 20, confidence REAL NOT NULL DEFAULT .35, val_split REAL NOT NULL DEFAULT .2, capture_fps REAL NOT NULL DEFAULT 2, FOREIGN KEY(camera_id) REFERENCES cameras(id));
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, login TEXT UNIQUE NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS auth_sessions(id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS auth_recovery_codes(id TEXT PRIMARY KEY, email TEXT NOT NULL, code_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used_at TEXT NOT NULL DEFAULT '');
    """)
    camera_columns={r[1] for r in con.execute("PRAGMA table_info(cameras)").fetchall()}
    for column,ddl in {"description":"TEXT NOT NULL DEFAULT ''","fps_limit":"REAL NOT NULL DEFAULT 8","created_at":"TEXT NOT NULL DEFAULT ''","telemetry_at":"TEXT NOT NULL DEFAULT ''","last_error":"TEXT NOT NULL DEFAULT ''","restart_requested_at":"TEXT NOT NULL DEFAULT ''"}.items():
        if column not in camera_columns: con.execute(f"ALTER TABLE cameras ADD COLUMN {column} {ddl}")
    con.execute("UPDATE cameras SET created_at=updated_at WHERE created_at='' OR created_at IS NULL")
    # A live MJPEG browser stream has a deliberate upper bound: keep stored
    # legacy values consistent with the UI/API maximum of 60 FPS.
    con.execute("UPDATE cameras SET fps_limit=60 WHERE fps_limit>60")
    # Previous releases capped every camera at 20 FPS. High-FPS mode upgrades
    # the known legacy 8/20 FPS defaults to 60 while the UI still exposes a per-camera choice.
    if HIGH_FPS_MODE: con.execute("UPDATE cameras SET fps_limit=60 WHERE fps_limit IN (8,20)")
    # The inference worker reports the exact lifecycle of the active model so
    # the Web panel can distinguish "selected" from "actually loaded".
    worker_columns={r[1] for r in con.execute("PRAGMA table_info(worker_status)").fetchall()}
    for column,ddl in {"model_name":"TEXT NOT NULL DEFAULT ''","model_status":"TEXT NOT NULL DEFAULT 'none'","model_error":"TEXT NOT NULL DEFAULT ''"}.items():
        if column not in worker_columns: con.execute(f"ALTER TABLE worker_status ADD COLUMN {column} {ddl}")
    model_columns={r[1] for r in con.execute("PRAGMA table_info(model_registry)").fetchall()}
    for column,ddl in {"artifact_uri":"TEXT NOT NULL DEFAULT ''","checksum":"TEXT NOT NULL DEFAULT ''"}.items():
        if column not in model_columns: con.execute(f"ALTER TABLE model_registry ADD COLUMN {column} {ddl}")
    training_columns={r[1] for r in con.execute("PRAGMA table_info(training_jobs)").fetchall()}
    for column,ddl in {"batch":"INTEGER NOT NULL DEFAULT 8","imgsz":"INTEGER NOT NULL DEFAULT 640","patience":"INTEGER NOT NULL DEFAULT 20","confidence":"REAL NOT NULL DEFAULT .35","val_split":"REAL NOT NULL DEFAULT .2","capture_fps":"REAL NOT NULL DEFAULT 2","source":"TEXT NOT NULL DEFAULT 'camera'","dataset_name":"TEXT NOT NULL DEFAULT ''"}.items():
        if column not in training_columns: con.execute(f"ALTER TABLE training_jobs ADD COLUMN {column} {ddl}")
    con.execute("CREATE TABLE IF NOT EXISTS datasets(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, path TEXT NOT NULL, image_count INTEGER NOT NULL DEFAULT 0, class_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'yolo', media_count INTEGER NOT NULL DEFAULT 0, label_count INTEGER NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE IF NOT EXISTS dataset_capture_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, camera_id TEXT NOT NULL, target_count INTEGER NOT NULL, capture_fps REAL NOT NULL, status TEXT NOT NULL, captured_count INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(camera_id) REFERENCES cameras(id))")
    dataset_columns={r[1] for r in con.execute("PRAGMA table_info(datasets)").fetchall()}
    for column,ddl in {"kind":"TEXT NOT NULL DEFAULT 'yolo'","media_count":"INTEGER NOT NULL DEFAULT 0","label_count":"INTEGER NOT NULL DEFAULT 0"}.items():
        if column not in dataset_columns: con.execute(f"ALTER TABLE datasets ADD COLUMN {column} {ddl}")
    event_columns={r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
    if "external_id" not in event_columns: con.execute("ALTER TABLE events ADD COLUMN external_id TEXT")
    if "review_status" not in event_columns:
        con.execute("ALTER TABLE events ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'")
        con.execute("UPDATE events SET review_status=CASE WHEN acknowledged=1 THEN 'accepted' ELSE 'pending' END")
    if "reviewed_at" not in event_columns: con.execute("ALTER TABLE events ADD COLUMN reviewed_at TEXT NOT NULL DEFAULT ''")
    con.execute("UPDATE events SET review_status='pending' WHERE review_status NOT IN ('pending','accepted','rejected') OR review_status='' OR review_status IS NULL")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_events_external_id ON events(external_id) WHERE external_id IS NOT NULL")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_timestamp ON events(timestamp DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_camera_timestamp ON events(camera_id,timestamp DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_severity_ack ON events(severity,acknowledged)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_review_status ON events(review_status,timestamp DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_logs_timestamp_level ON logs(timestamp DESC,level)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_training_status ON training_jobs(status,created_at DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_capture_jobs_status ON dataset_capture_jobs(status,created_at DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_auth_sessions_expires ON auth_sessions(expires_at)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_auth_recovery_email_expires ON auth_recovery_codes(email,expires_at)")
    con.execute("CREATE TABLE IF NOT EXISTS bot_runtime(provider TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'absent', detail TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '')")
    con.execute("CREATE TABLE IF NOT EXISTS bot_commands(id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT '')")
    con.execute("CREATE INDEX IF NOT EXISTS ix_bot_commands_provider_status ON bot_commands(provider,status,id DESC)")
    con.execute("UPDATE training_jobs SET status='failed',stage='Прервано перезапуском',error='Worker restarted before completion',updated_at=? WHERE status IN ('queued','running')",(now_iso(),))
    legacy_telegram_enabled_row=con.execute("SELECT value FROM settings WHERE key='telegram_enabled'").fetchone()
    legacy_telegram_chats_row=con.execute("SELECT value FROM settings WHERE key='telegram_chat_ids'").fetchone()
    legacy_critical_alerts_row=con.execute("SELECT value FROM settings WHERE key='critical_alerts'").fetchone()
    legacy_telegram_enabled=bool(legacy_telegram_enabled_row and legacy_telegram_enabled_row[0]=='true')
    legacy_critical_alerts=bool(legacy_critical_alerts_row and legacy_critical_alerts_row[0]=='true')
    config_defaults={
        "active_model":"", "active_model_slots":"{}", "active_model_disabled":"false", "ppe_trial_previous_model":"", "model_test_mode":"false", "auth_email":"", "auth_password_must_change":str(True).lower(), "site_name":"ZMK Vision", "timezone":"Asia/Krasnoyarsk", "language":"ru",
        "retention_days":"90", "archive_quality":"90", "archive_clip_seconds":"10",
        "inference_fps":"8", "inference_device":"cuda:0", "batch_size":"4", "nms_iou":"0.45", "model_test_conf":str(MODEL_TEST_CONF_DEFAULT),
        "helmet_conf":"0.85", "vest_conf":"0.80", "phone_conf":"0.78", "smoking_conf":"0.80", "restricted_zone_conf":"0.82", "immobility_conf":"0.80", "min_model_precision":"90", "min_model_recall":"85",
        "telegram_enabled":"true" if legacy_telegram_enabled else "false", "telegram_chat_ids":legacy_telegram_chats_row[0] if legacy_telegram_chats_row else "", "critical_alerts":"true",
        # Bot tokens are intentionally outside SQLite/settings responses. They
        # may be supplied from legacy .env values or written through Admin →
        # Bots to a private, owner-only secret volume/file.
        "telegram_bot_enabled":"true" if (legacy_telegram_enabled or MESSENGER_PROVIDER=="telegram") else "false",
        "telegram_alerts_enabled":"true" if (legacy_telegram_enabled or MESSENGER_PROVIDER=="telegram") else "false",
        "telegram_alert_min_severity":"critical" if legacy_critical_alerts else "high",
        "telegram_admin_ids":_telegram_role_env("ADMIN"), "telegram_operator_ids":_telegram_role_env("OPERATOR"), "telegram_viewer_ids":_telegram_role_env("VIEWER"), "telegram_alert_recipients":legacy_telegram_chats_row[0] if legacy_telegram_chats_row else "", "telegram_webapp_url":os.getenv("TELEGRAM_WEBAPP_URL", "").strip(),
        "max_bot_enabled":"true" if MESSENGER_PROVIDER=="max" else "false",
        "max_alerts_enabled":"true" if MESSENGER_PROVIDER=="max" else "false",
        "max_alert_min_severity":"critical" if legacy_critical_alerts else "high",
        "max_admin_ids":os.getenv("MAX_ADMIN_IDS", "").strip(), "max_operator_ids":os.getenv("MAX_OPERATOR_IDS", "").strip(), "max_viewer_ids":os.getenv("MAX_VIEWER_IDS", "").strip(), "max_alert_recipients":"",
        "webhook_enabled":"false", "webhook_url":"", "webhook_timeout":"5",
        "minio_endpoint":"minio:9000", "minio_bucket":"videoanalytics", "minio_secure":"false",
        "rtsp_reconnect_seconds":"5", "event_cooldown_seconds":"30"
    }
    for key,value in config_defaults.items(): con.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",(key,value))
    _initialize_or_upgrade_auth_password(con)
    con.execute("DELETE FROM auth_sessions WHERE expires_at<=?",(now_iso(),))
    con.execute("DELETE FROM auth_recovery_codes WHERE expires_at<=? OR used_at!=''",(now_iso(),))
    disabled_row=con.execute("SELECT value FROM settings WHERE key='active_model_disabled'").fetchone()
    if disabled_row and disabled_row[0]=='true':
        # No model is active after an explicitly stopped PPE trial. Do not
        # resurrect a ready model merely because the API/container restarted.
        con.execute("UPDATE settings SET value='' WHERE key='active_model'")
        con.execute("UPDATE settings SET value='false' WHERE key='model_test_mode'")
    else:
        active_row=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()
        # An empty value means the operator has not selected a model. Never
        # auto-start a newly downloaded (and possibly unvalidated) preset just
        # because the API restarted. Only repair a *previously selected* model
        # that disappeared or became invalid.
        if active_row and active_row[0]:
            active_ok=con.execute("SELECT 1 FROM model_registry WHERE name=? AND status='ready'",(active_row[0],)).fetchone()
            if not active_ok:
                fallback=con.execute("SELECT name FROM model_registry WHERE status='ready' ORDER BY id DESC LIMIT 1").fetchone()
                value=fallback[0] if fallback else ""
                con.execute("INSERT INTO settings(key,value) VALUES('active_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(value,))
                con.execute("UPDATE settings SET value='false' WHERE key='model_test_mode'")
                if fallback: con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","model_manager",f"Active model repaired to {fallback[0]}"))
    bootstrap_env_camera(con)
    apply_retention(con)
    con.commit(); con.close()

class AuthLoginIn(BaseModel):
    password:str=Field(min_length=1,max_length=128)
class AuthPasswordIn(BaseModel):
    current_password:str=Field(min_length=1,max_length=128)
    new_password:str=Field(min_length=4,max_length=128)
class AuthEmailIn(BaseModel):
    email:str=Field(min_length=5,max_length=254)
    password:str=Field(min_length=1,max_length=128)
    @field_validator("email")
    @classmethod
    def validate_email(cls,value:str):
        value=value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",value): raise ValueError("Некорректный email")
        return value
class AuthRecoveryRequestIn(BaseModel):
    email:str=Field(min_length=5,max_length=254)
    @field_validator("email")
    @classmethod
    def validate_recovery_email(cls,value:str):
        value=value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",value): raise ValueError("Некорректный email")
        return value
class AuthRecoveryVerifyIn(AuthRecoveryRequestIn):
    code:str=Field(min_length=6,max_length=6,pattern=r"^\d{6}$")
    new_password:str=Field(min_length=4,max_length=128)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Push the configured camera list into go2rtc as soon as the API starts.
    # go2rtc may still be starting; sync is idempotent and is repeated on every
    # camera CRUD operation and on a low-frequency background reconcile.
    sync_go2rtc_cameras()
    sync_task: asyncio.Task[None] | None = None
    if GO2RTC_ENABLED and GO2RTC_API_URL:
        async def _go2rtc_sync_loop() -> None:
            while True:
                await asyncio.sleep(15)
                await asyncio.to_thread(sync_go2rtc_cameras)
        sync_task = asyncio.create_task(_go2rtc_sync_loop(), name="go2rtc-camera-sync")
    try: yield
    finally:
        if sync_task is not None and not sync_task.done():
            sync_task.cancel()
        for task in [*list(_training_tasks.values()),*list(_dataset_capture_tasks.values())]: task.cancel()
        pending=[*list(_training_tasks.values()),*list(_dataset_capture_tasks.values())]
        if sync_task is not None:
            pending.append(sync_task)
        if pending: await asyncio.gather(*pending,return_exceptions=True)

app=FastAPI(title="ZMK Vision API",version=APP_VERSION,description="On-premise API контура видеоаналитики",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[x.strip() for x in os.getenv("CORS_ORIGINS","http://localhost:5173").split(",") if x.strip()],allow_credentials=False,allow_methods=["GET","POST","PUT","PATCH","DELETE"],allow_headers=["Content-Type","X-API-Key","X-Telegram-Init-Data"])

def custom_openapi():
    if app.openapi_schema: return app.openapi_schema
    schema=get_openapi(title=app.title,version=app.version,description=app.description,routes=app.routes)
    schema.setdefault("components",{}).setdefault("securitySchemes",{})["ApiKeyAuth"]={"type":"apiKey","in":"header","name":"X-API-Key"}
    for path,methods in schema.get("paths",{}).items():
        if path.startswith("/api/") and path not in {"/api/health","/api/auth/status","/api/auth/login","/api/auth/recovery/request","/api/auth/recovery/verify"}:
            for operation in methods.values():
                if isinstance(operation,dict): operation["security"]=[{"ApiKeyAuth":[]}]
    app.openapi_schema=schema; return schema
app.openapi=custom_openapi

def telegram_webapp_role(init_data:str)->str|None:
    """Validate Telegram Mini App initData and return the whitelisted role."""
    # Resolve on each request: an Admin-entered token must authenticate the
    # Mini App immediately and the secret itself is never sent to the browser.
    token=_effective_bot_token("telegram")
    if not token or not init_data or len(init_data)>8192: return None
    try:
        values=dict(parse_qsl(init_data,keep_blank_values=True)); supplied=values.pop("hash","")
        auth_date=int(values.get("auth_date","0"))
        if abs(int(time.time())-auth_date)>3600: return None
        check="\n".join(f"{k}={v}" for k,v in sorted(values.items()))
        secret=hmac.new(b"WebAppData",token.encode(),hashlib.sha256).digest()
        expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied,expected): return None
        user=json.loads(values.get("user","{}")); role=_bot_role_for_user("telegram",int(user.get("id",0)),str(user.get("username") or "")); return role if role in {"viewer","operator","admin"} else None
    except (ValueError,TypeError,json.JSONDecodeError): return None

@app.get("/api/session")
def session_info(request: Request):
    """Minimal authenticated identity for the Telegram Mini App and web shell."""
    init_data=request.headers.get("X-Telegram-Init-Data","")
    role=telegram_webapp_role(init_data)
    api_key_ok=bool(API_KEY and hmac.compare_digest(request.headers.get("X-API-Key",""),API_KEY))
    password_session=_auth_session(request) if PASSWORD_AUTH_ENABLED else None
    user={}
    if role:
        try:
            values=dict(parse_qsl(init_data,keep_blank_values=True))
            raw=json.loads(values.get("user","{}"))
            user={"id":int(raw.get("id",0)),"name":str(raw.get("first_name") or raw.get("username") or "Telegram")[:80]}
        except (TypeError,ValueError,json.JSONDecodeError):
            user={}
    return {"telegram":bool(role),"authenticated":bool(role or api_key_ok or password_session or not PASSWORD_AUTH_ENABLED),"role":role or ("api_key" if api_key_ok else "password" if password_session else "local"),"user":user}

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Optional API-key protection, size/rate limits and baseline response headers."""
    path=request.url.path
    public=path in {"/api/health","/docs","/openapi.json","/redoc"} or not path.startswith("/api/")
    worker_token_ok=bool(WORKER_TOKEN and hmac.compare_digest(request.headers.get("X-Worker-Token",""),WORKER_TOKEN))
    if path.startswith("/api/internal/"):
        from fastapi.responses import JSONResponse
        # A worker token is auto-provisioned on the shared model-data volume.
        # We only 503 if it could not be provisioned at all (e.g. volume
        # read-only). Otherwise we require it strictly (constant-time).
        if not WORKER_TOKEN:
            return JSONResponse({"detail":"Worker token could not be provisioned: ensure model-data volume is writable"},status_code=503)
        if not worker_token_ok: return JSONResponse({"detail":"Invalid worker token"},status_code=401)
    api_key_ok=bool(API_KEY and hmac.compare_digest(request.headers.get("X-API-Key",""),API_KEY))
    telegram_role=telegram_webapp_role(request.headers.get("X-Telegram-Init-Data",""))
    bot_service_ok=bool(BOT_API_TOKEN and hmac.compare_digest(request.headers.get("X-Bot-Service-Token",""),BOT_API_TOKEN))
    auth_public=path in {"/api/auth/status","/api/auth/login","/api/auth/recovery/request","/api/auth/recovery/verify"}
    password_session=_auth_session(request) if PASSWORD_AUTH_ENABLED and path.startswith("/api/") else None
    session_ok=bool(password_session)
    request.state.password_session=password_session
    worker_service_ok=worker_token_ok and path.startswith(("/api/internal/","/api/inference/"))
    access_ok=api_key_ok or bool(telegram_role) or bot_service_ok or session_ok or worker_service_ok
    if API_KEY and not public and not auth_public and not access_ok:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail":"Invalid or missing API credentials"},status_code=401,headers={"WWW-Authenticate":"ApiKey"})
    if PASSWORD_AUTH_ENABLED and path.startswith("/api/") and not public and not auth_public and not access_ok:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail":"Password authentication required"},status_code=401,headers={"WWW-Authenticate":"Session"})
    if PASSWORD_AUTH_ENABLED and session_ok and not (api_key_ok or telegram_role or bot_service_ok) and _auth_setting("auth_password_must_change","false")=="true" and path.startswith("/api/") and path not in {"/api/auth/status","/api/auth/password","/api/auth/logout"}:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail":"Change the initial password before using the system"},status_code=403)
    if telegram_role and not api_key_ok and not bot_service_ok:
        admin_write=path.startswith(("/api/admin/","/api/bots/","/api/training/","/api/settings","/api/models/")) and request.method!="GET"
        admin_read=path.startswith(("/api/admin/","/api/bots/","/api/logs","/api/settings"))
        operator_only=path.startswith("/api/reports/")
        viewer_write=telegram_role=="viewer" and request.method!="GET"
        operator_write=telegram_role=="operator" and request.method!="GET" and not (path.startswith("/api/events/") and (path.endswith(("/ack","/reject")) or path in {"/api/events/ack-bulk","/api/events/reject-bulk"}))
        if (telegram_role!="admin" and (admin_write or admin_read)) or (telegram_role=="viewer" and operator_only) or viewer_write or operator_write:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail":"Insufficient Telegram role"},status_code=403)
    length=request.headers.get("content-length")
    # Dataset ZIPs and model artifacts are streamed directly to persistent
    # volumes and can be substantially larger than normal JSON API payloads.
    # The handlers enforce the same ceiling again while reading chunks, which
    # also covers clients using chunked transfer without Content-Length.
    if path=="/api/models/upload" and request.method=="POST": cap=MODEL_UPLOAD_MAX_BYTES
    elif path.startswith("/api/datasets") and request.method=="POST": cap=512_000_000
    else: cap=2_000_000
    try: too_large=bool(length and int(length)>cap)
    except ValueError: too_large=True
    if too_large:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail":"Request body too large"},status_code=413)
    if path.startswith(("/api/admin/","/api/inference/")):
        now=time.time(); client_id=request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown"); key=f"{client_id}:{path.split('/')[2]}"
        if len(_rate_buckets)>=10_000 and key not in _rate_buckets:
            for old_key in [k for k,v in _rate_buckets.items() if not v or now-v[-1]>=60]: _rate_buckets.pop(old_key,None)
            if len(_rate_buckets)>=10_000:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail":"Rate limiter capacity exceeded"},status_code=429,headers={"Retry-After":"60"})
        bucket=[x for x in _rate_buckets.get(key,[]) if now-x<60]
        if len(bucket)>=RATE_LIMIT_PER_MINUTE:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail":"Rate limit exceeded"},status_code=429,headers={"Retry-After":"60"})
        bucket.append(now); _rate_buckets[key]=bucket
    response=await call_next(request)
    response.headers.update({"X-Content-Type-Options":"nosniff","X-Frame-Options":"SAMEORIGIN","Referrer-Policy":"no-referrer","Permissions-Policy":"camera=(), microphone=(), geolocation=()"})
    if path.startswith("/api/"): response.headers["Cache-Control"]="no-store"
    return response

@app.get("/api/auth/status")
def auth_status(request: Request):
    session=_auth_session(request) if PASSWORD_AUTH_ENABLED else None
    api_key_ok=bool(API_KEY and hmac.compare_digest(request.headers.get("X-API-Key",""),API_KEY))
    email=_auth_setting("auth_email","") if PASSWORD_AUTH_ENABLED else ""
    return {"enabled":PASSWORD_AUTH_ENABLED,"authenticated":bool(session or api_key_ok or not PASSWORD_AUTH_ENABLED),"must_change":_auth_setting("auth_password_must_change","false")=="true" if PASSWORD_AUTH_ENABLED and not api_key_ok else False,"email_bound":bool(email),"email":_masked_email(email),"recovery_available":_smtp_ready(),"expires_at":session.get("expires_at","") if session else ""}


@app.post("/api/auth/login")
def auth_login(payload: AuthLoginIn, request: Request, response: Response):
    if not PASSWORD_AUTH_ENABLED:
        raise HTTPException(409,"Парольный вход отключён администратором")
    if not _allow_auth_attempt(request):
        raise HTTPException(429,"Слишком много попыток. Повторите через 10 минут.")
    encoded=_auth_setting("auth_password_hash","")
    if not _password_matches(payload.password,encoded):
        con=db(); con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","auth","Failed password login")); con.commit(); con.close()
        raise HTTPException(401,"Неверный пароль")
    token,expires=_create_auth_session()
    _set_auth_cookie(response,token,request)
    con=db(); con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","auth","Password login succeeded")); con.commit(); con.close()
    return {"authenticated":True,"must_change":_auth_setting("auth_password_must_change","false")=="true","expires_at":expires}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    session=_auth_session(request)
    if session:
        con=db(); con.execute("DELETE FROM auth_sessions WHERE id=?",(session["id"],)); con.commit(); con.close()
    response.delete_cookie(AUTH_COOKIE_NAME,path="/")
    return {"logged_out":True}


def _require_password_session(request: Request) -> dict[str,Any]:
    if not PASSWORD_AUTH_ENABLED:
        raise HTTPException(409,"Парольный вход отключён администратором")
    session=_auth_session(request)
    if not session:
        raise HTTPException(401,"Требуется вход по паролю")
    return session


@app.get("/api/auth/sessions")
def auth_sessions(request: Request):
    current=_require_password_session(request)
    con=db()
    try:
        data=[]
        for row in con.execute("SELECT id,created_at,expires_at,last_seen_at FROM auth_sessions WHERE expires_at>? ORDER BY last_seen_at DESC,created_at DESC",(now_iso(),)).fetchall():
            data.append({"id":str(row[0]),"created_at":str(row[1]),"expires_at":str(row[2]),"last_seen_at":str(row[3]),"current":hmac.compare_digest(str(row[0]),current["id"])})
        return {"sessions":data}
    finally:
        con.close()


@app.post("/api/auth/sessions/revoke-others")
def auth_revoke_other_sessions(request: Request):
    current=_require_password_session(request)
    con=db()
    try:
        cur=con.execute("DELETE FROM auth_sessions WHERE id!=?",(current["id"],))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","auth",f"Other password sessions revoked: {cur.rowcount}"))
        con.commit()
        return {"revoked":cur.rowcount}
    finally:
        con.close()


@app.delete("/api/auth/sessions/{session_id}")
def auth_revoke_session(session_id: str, request: Request, response: Response):
    current=_require_password_session(request)
    if not re.fullmatch(r"[a-f0-9]{32}",session_id):
        raise HTTPException(404,"Сеанс не найден")
    con=db()
    try:
        cur=con.execute("DELETE FROM auth_sessions WHERE id=?",(session_id,))
        if not cur.rowcount:
            raise HTTPException(404,"Сеанс не найден")
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","auth","Password session revoked"))
        con.commit()
    finally:
        con.close()
    current_deleted=hmac.compare_digest(session_id,current["id"])
    if current_deleted:
        response.delete_cookie(AUTH_COOKIE_NAME,path="/")
    return {"revoked":True,"current":current_deleted}


@app.put("/api/auth/password")
def auth_change_password(payload: AuthPasswordIn, request: Request, response: Response):
    _require_password_session(request)
    encoded=_auth_setting("auth_password_hash","")
    if not _password_matches(payload.current_password,encoded):
        raise HTTPException(401,"Текущий пароль неверный")
    if hmac.compare_digest(payload.current_password,payload.new_password):
        raise HTTPException(422,"Новый пароль должен отличаться от текущего")
    con=db(); con.execute("INSERT INTO settings(key,value) VALUES('auth_password_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(_hash_password(payload.new_password),)); con.execute("INSERT INTO settings(key,value) VALUES('auth_password_must_change','false') ON CONFLICT(key) DO UPDATE SET value=excluded.value"); con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","auth","Password changed")); con.commit(); con.close()
    _revoke_auth_sessions()
    token,expires=_create_auth_session()
    _set_auth_cookie(response,token,request)
    return {"changed":True,"expires_at":expires}


@app.put("/api/auth/email")
def auth_bind_email(payload: AuthEmailIn, request: Request):
    _require_password_session(request)
    if not _password_matches(payload.password,_auth_setting("auth_password_hash","") ):
        raise HTTPException(401,"Пароль неверный")
    con=db(); con.execute("INSERT INTO settings(key,value) VALUES('auth_email',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(payload.email,)); con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","auth","Recovery email bound")); con.commit(); con.close()
    return {"email":_masked_email(payload.email),"recovery_available":_smtp_ready()}


@app.post("/api/auth/recovery/request")
def auth_recovery_request(payload: AuthRecoveryRequestIn, request: Request):
    if not PASSWORD_AUTH_ENABLED:
        raise HTTPException(409,"Парольный вход отключён администратором")
    email=_auth_setting("auth_email","")
    # Do not reveal whether a different address is bound.
    if not email or not hmac.compare_digest(email,payload.email):
        return {"accepted":True,"message":"Если адрес привязан, код отправлен."}
    if not _smtp_ready():
        raise HTTPException(503,"SMTP не настроен. Войдите локально и настройте почту/SMTP.")
    if not _allow_auth_attempt(request):
        raise HTTPException(429,"Слишком много запросов. Повторите через 10 минут.")
    code=f"{secrets.randbelow(1_000_000):06d}"
    expires=(datetime.now(TZ)+timedelta(minutes=AUTH_RECOVERY_MINUTES)).isoformat(timespec="seconds")
    recovery_id=uuid.uuid4().hex
    con=db()
    try:
        con.execute("DELETE FROM auth_recovery_codes WHERE email=?",(email,))
        con.execute("INSERT INTO auth_recovery_codes(id,email,code_hash,created_at,expires_at,attempts,used_at) VALUES(?,?,?,?,?,?,?)",(recovery_id,email,hashlib.sha256(code.encode()).hexdigest(),now_iso(),expires,0,""))
        con.commit()
    finally:
        con.close()
    try:
        _send_recovery_email(email,code)
    except Exception as exc:
        con=db(); con.execute("DELETE FROM auth_recovery_codes WHERE id=?",(recovery_id,)); con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"ERROR","auth",f"Recovery email failed: {type(exc).__name__}")); con.commit(); con.close()
        raise HTTPException(502,"Не удалось отправить письмо с кодом") from exc
    con=db(); con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","auth","Password recovery code sent")); con.commit(); con.close()
    return {"accepted":True,"message":f"Код отправлен. Он действует {AUTH_RECOVERY_MINUTES} минут."}


@app.post("/api/auth/recovery/verify")
def auth_recovery_verify(payload: AuthRecoveryVerifyIn, request: Request, response: Response):
    if not PASSWORD_AUTH_ENABLED:
        raise HTTPException(409,"Парольный вход отключён администратором")
    email=_auth_setting("auth_email","")
    if not email or not hmac.compare_digest(email,payload.email):
        raise HTTPException(400,"Код недействителен или истёк")
    con=db()
    try:
        row=con.execute("SELECT id,code_hash,expires_at,attempts FROM auth_recovery_codes WHERE email=? AND used_at='' ORDER BY created_at DESC LIMIT 1",(email,)).fetchone()
        if not row or str(row[2])<=now_iso() or int(row[3])>=5:
            raise HTTPException(400,"Код недействителен или истёк")
        actual=hashlib.sha256(payload.code.encode()).hexdigest()
        if not hmac.compare_digest(actual,str(row[1])):
            con.execute("UPDATE auth_recovery_codes SET attempts=attempts+1 WHERE id=?",(row[0],)); con.commit()
            raise HTTPException(400,"Код недействителен или истёк")
        con.execute("UPDATE auth_recovery_codes SET used_at=? WHERE id=?",(now_iso(),row[0]))
        con.execute("INSERT INTO settings(key,value) VALUES('auth_password_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(_hash_password(payload.new_password),))
        con.execute("INSERT INTO settings(key,value) VALUES('auth_password_must_change','false') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","auth","Password reset by recovery code"))
        con.commit()
    finally:
        con.close()
    _revoke_auth_sessions()
    token,expires=_create_auth_session()
    _set_auth_cookie(response,token,request)
    return {"reset":True,"expires_at":expires}


def safe_camera_error(value: str) -> str:
    """Keep diagnostic text useful without exposing RTSP credentials."""
    return re.sub(r"rtsps?://[^\s'\"<>]+","<rtsp-url>",value or "",flags=re.IGNORECASE).replace("\n"," ")[:300]

def normalize_rtsp_url(value: str | None) -> str | None:
    """Validate an RTSP endpoint early, without ever returning its secret.

    A prefix-only check accepts values such as ``rtsp://host:not-a-port``.
    They later crash diagnostics through ``urlparse(...).port`` and leave an
    operator with a 500 instead of an actionable configuration error.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return value
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("RTSP URL не должен содержать пробелы или управляющие символы")
    parsed = urlparse(value)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise ValueError("Требуется корректный RTSP(S) URL с host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Некорректный порт в RTSP URL") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Порт RTSP URL должен быть в диапазоне 1–65535")
    return value


def bootstrap_env_camera(con: sqlite3.Connection) -> None:
    """Create the first camera from RTSP_CAM_01 when the DB has none.

    Docker Compose already passed RTSP_CAM_01 into the API but it was never
    consumed, so a user could configure a valid URL in .env and the worker
    would silently receive an empty camera list. The URL remains secret: it is
    stored only in SQLite and never written to logs or camera-list responses.
    """
    raw_url = os.getenv("RTSP_CAM_01", "").strip()
    if not raw_url:
        return
    if con.execute("SELECT 1 FROM cameras WHERE rtsp_url!='' LIMIT 1").fetchone():
        return
    try:
        rtsp_url = normalize_rtsp_url(raw_url)
    except ValueError:
        con.execute(
            "INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",
            (now_iso(), "WARNING", "camera_manager", "RTSP_CAM_01 ignored: invalid RTSP URL"),
        )
        return

    timestamp = now_iso()
    camera_id = "cam_env_01"
    existing = con.execute("SELECT rtsp_url FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if existing:
        # Never overwrite an already configured camera. A legacy empty
        # bootstrap row can safely receive the newly supplied environment URL.
        if existing[0]:
            return
        con.execute(
            "UPDATE cameras SET rtsp_url=?,enabled=1,status='connecting',fps=0,latency_ms=0,last_error='',updated_at=?,telemetry_at='',restart_requested_at=? WHERE id=?",
            (rtsp_url, timestamp, timestamp, camera_id),
        )
    else:
        con.execute(
            "INSERT INTO cameras(id,name,zone,description,rtsp_url,fps_limit,status,fps,latency_ms,enabled,created_at,updated_at,telemetry_at,restart_requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (camera_id, "Камера 01", "Без зоны", "Добавлена из RTSP_CAM_01", rtsp_url, 30, "connecting", 0, 0, 1, timestamp, timestamp, "", timestamp),
        )
    con.execute(
        "INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",
        (timestamp, "INFO", "camera_manager", "Camera created from RTSP_CAM_01", camera_id),
    )


class CameraIn(BaseModel):
    name:str=Field(min_length=2,max_length=80)
    zone:str=Field(default="Без зоны",min_length=1,max_length=80)
    description:str=Field(default="",max_length=500)
    rtsp_url:str=Field(default="",max_length=2048)
    fps_limit:float=Field(default=30,ge=.1,le=60)
    enabled:bool=True
    @field_validator("rtsp_url")
    @classmethod
    def validate_rtsp(cls,value:str):
        return normalize_rtsp_url(value) or ""
class CameraUpdate(BaseModel):
    name:str=Field(min_length=2,max_length=80)
    zone:str=Field(default="Без зоны",min_length=1,max_length=80)
    description:str=Field(default="",max_length=500)
    rtsp_url:str|None=Field(default=None,max_length=2048)
    fps_limit:float=Field(default=30,ge=.1,le=60)
    enabled:bool=True
    @field_validator("rtsp_url")
    @classmethod
    def validate_rtsp(cls,value:str|None):
        return normalize_rtsp_url(value)
class CameraTelemetry(BaseModel):
    status:Literal["connecting","online","offline","error","unknown","recovering"]
    fps:float=Field(default=0,ge=0,le=240)
    latency_ms:int=Field(default=0,ge=0,le=120000)
    error:str=Field(default="",max_length=300)
class InferenceHeartbeat(BaseModel):
    status:Literal["starting","running","idle","degraded","stopped"]
    detail:str=Field(default="",max_length=300)
    camera_count:int=Field(default=0,ge=0,le=10000)
    model_name:str=Field(default="",max_length=120)
    model_status:Literal["none","loading","ready","error"]="none"
    model_error:str=Field(default="",max_length=300)
class CameraSnapshotIn(BaseModel):
    jpeg_base64:str=Field(min_length=16,max_length=1_800_000)
    captured_at:datetime|None=None
class SettingIn(BaseModel): value:float=Field(ge=.1,le=1)
class AckIn(BaseModel): note:str=Field(default="",max_length=500)
class BulkAckIn(BaseModel):
    event_ids:list[int]=Field(min_length=1,max_length=500)
    note:str=Field(default="",max_length=500)
    @field_validator("event_ids")
    @classmethod
    def unique_positive_ids(cls,value:list[int]):
        if any(item<1 for item in value): raise ValueError("event_ids must be positive")
        return list(dict.fromkeys(value))
class DetectionIn(BaseModel):
    camera_id:str=Field(min_length=1,max_length=64)
    model_name:str=Field(min_length=1,max_length=120)
    timestamp:datetime|None=None
    event_type:Literal["no_helmet","no_vest","phone_usage","smoking","restricted_zone","immobility"]
    confidence:float=Field(ge=0,le=1)
    person_id:str|None=Field(default=None,max_length=120)
    detection_id:str|None=Field(default=None,min_length=8,max_length=160,pattern=r"^[a-zA-Z0-9._:-]+$")
    bbox:list[float]=Field(default_factory=list,min_length=0,max_length=4)
    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls,value:list[float]):
        if not value: return value
        if len(value)!=4: raise ValueError("bbox must be empty or [x1,y1,x2,y2]")
        if any(x<0 for x in value) or value[2]<=value[0] or value[3]<=value[1]: raise ValueError("bbox coordinates are invalid")
        return value
class DetectionBatch(BaseModel): detections:list[DetectionIn]=Field(min_length=1,max_length=500)
class ConfigPatch(BaseModel): values:dict[str,Any]
class BotConfigIn(BaseModel):
    enabled:bool=False
    alerts_enabled:bool=False
    alert_min_severity:Literal["critical","high","medium","low"]="high"
    admin_ids:str=Field(default="",max_length=4000)
    operator_ids:str=Field(default="",max_length=4000)
    viewer_ids:str=Field(default="",max_length=4000)
    alert_recipients:str=Field(default="",max_length=4000)
    webapp_url:str=Field(default="",max_length=1000)
    # Write-only on purpose.  Keep this untyped here so validation never
    # echoes a malformed secret back in FastAPI's 422 response.
    token:Any|None=Field(default=None,json_schema_extra={"writeOnly":True})
class BotHeartbeatIn(BaseModel):
    status:Literal["active","disabled","waiting_token","api_unavailable","error"]
    detail:str=Field(default="",max_length=300)
    enabled:bool=False
class BotCommandCompleteIn(BaseModel):
    status:Literal["completed","failed"]
    error:str=Field(default="",max_length=300)
class UserIn(BaseModel):
    name:str=Field(min_length=2,max_length=80)
    login:str=Field(min_length=2,max_length=40,pattern=r"^[a-zA-Z0-9._-]+$")
    role:Literal["admin","operator","viewer"]
class ModelIn(BaseModel):
    name:str=Field(min_length=2,max_length=120,pattern=r"^[a-zA-Z0-9._-]+$")
    format:Literal["ONNX","ONNX FP16","TensorRT","TensorRT FP16","PyTorch"]
    # Validation metrics are optional for a camera test. They are required only
    # before the operator promotes a model into a production slot.
    precision:float|None=Field(default=None,ge=0,le=100)
    recall:float|None=Field(default=None,ge=0,le=100)
    source:str=Field(default="external",max_length=200)
    artifact_uri:str=Field(min_length=1,max_length=1000)
    checksum:str=Field(default="",max_length=128,pattern=r"^[a-fA-F0-9]*$")
    @model_validator(mode="after")
    def metrics_are_a_pair(self):
        if (self.precision is None) != (self.recall is None):
            raise ValueError("Укажите Precision и Recall вместе или оставьте оба поля пустыми для теста на камере")
        return self
class ModelSlotIn(BaseModel):
    role:Literal["people","helmet","workwear","phone","smoking","zone"]
class ModelBulkDeleteIn(BaseModel):
    names:list[str]=Field(min_length=1,max_length=200)
    deactivate_active:bool=True
    @field_validator("names")
    @classmethod
    def validate_names(cls,value:list[str]):
        unique=list(dict.fromkeys(value))
        if any(not re.fullmatch(r"[A-Za-z0-9._-]{2,120}",name or "") for name in unique):
            raise ValueError("Недопустимое имя модели")
        return unique
class TrainingProgress(BaseModel):
    status:Literal["queued","running","completed","failed","cancelled"]
    progress:int=Field(ge=0,le=100)
    stage:str=Field(max_length=200)
    error:str|None=Field(default=None,max_length=500)
    artifact_uri:str|None=Field(default=None,max_length=1000)
    precision:float|None=Field(default=None,ge=0,le=100)
    recall:float|None=Field(default=None,ge=0,le=100)
class TrainingIn(BaseModel):
    camera_id:str
    image_count:int=Field(default=100,ge=20,le=5000)
    epochs:int=Field(default=20,ge=1,le=300)
    target_name:str|None=Field(default=None,min_length=2,max_length=120,pattern=r"^[a-zA-Z0-9._-]+$")
    batch:int=Field(default=8,ge=1,le=128)
    imgsz:int=Field(default=640,ge=320,le=1920)
    patience:int=Field(default=20,ge=0,le=100)
    confidence:float=Field(default=.35,ge=.05,le=.95)
    val_split:float=Field(default=.2,ge=.1,le=.4)
    capture_fps:float=Field(default=2,ge=.1,le=10)
    source:Literal["camera","dataset"]="camera"
    dataset_name:str|None=Field(default=None,min_length=2,max_length=120)

def rows(query,args=()):
    con=db(); result=[dict(r) for r in con.execute(query,args).fetchall()]; con.close(); return result

def _go2rtc_source_url(rtsp_url: str) -> str:
    """Build a low-latency go2rtc source URL like VLC: TCP transport, no buffering.
    v2.14.0: Adds mp4 query for MSE/HLS low-latency and ensures single connection to camera.
    v2.14.2: Preserve operator-specified transport if already present (e.g. udp), else default to tcp.
    v2.15.0: Add backchannel=0 for low latency (disable audio backchannel), keep single conn.
    """
    if not rtsp_url:
        return rtsp_url
    base = rtsp_url.split("#")[0]
    existing_frag = rtsp_url.split("#", 1)[1] if "#" in rtsp_url else ""
    # Preserve existing transport if operator already specified it (test expects udp kept)
    if existing_frag and "transport=" in existing_frag:
        # Ensure backchannel disabled if not already set
        if "backchannel" not in existing_frag:
            return f"{base}#{existing_frag}&backchannel=0"
        return f"{base}#{existing_frag}"
    if not existing_frag:
        return f"{base}#transport={GO2RTC_RTSP_TRANSPORT}&backchannel=0"
    # Existing fragment without transport -> append tcp + backchannel
    return f"{base}#{existing_frag}&transport={GO2RTC_RTSP_TRANSPORT}&backchannel=0"

def sync_go2rtc_cameras() -> dict[str, Any]:
    """Mirror enabled cameras from SQLite into go2rtc with VLC-like low latency.
    v2.14.0 ARCHITECTURE: go2rtc is the ONLY RTSP client to camera.
    - Backend creates streams zmk-{id} and {id} in go2rtc (single connection to camera)
    - Inference worker pulls from go2rtc RTSP rtsp://host.docker.internal:8554/zmk-{id} (local, high FPS)
    - Frontend pulls from go2rtc via WebRTC H264 direct (true 25-60 FPS, no re-encode)
    This eliminates double RTSP connections that caused 4 FPS and constant reconnects.
    go2rtc is optional: a slow/absent relay must never break camera CRUD.
    """
    if not GO2RTC_ENABLED or not GO2RTC_API_URL:
        return {"ok": False, "reason": "go2rtc disabled"}
    desired = rows("SELECT id,rtsp_url FROM cameras WHERE enabled=1 AND rtsp_url<>''")
    desired_map: dict[str, str] = {}
    for row in desired:
        cid = str(row["id"])
        url = str(row["rtsp_url"])
        # Primary name zmk-{id} used by inference worker and frontend
        desired_map[f"zmk-{cid}"] = url
        # Legacy plain {id} for backward compat with older frontends
        desired_map[cid] = url
    try:
        with httpx.Client(timeout=httpx.Timeout(GO2RTC_SYNC_TIMEOUT_SECONDS, connect=2.0)) as client:
            # Check if go2rtc is up
            try:
                existing_response = client.get(f"{GO2RTC_API_URL}/api/streams", timeout=3.0)
            except (httpx.ConnectError, httpx.ReadTimeout, OSError):
                return {"ok": False, "reason": "go2rtc not reachable (starting?)"}
            if existing_response.status_code >= 400:
                return {"ok": False, "reason": f"HTTP {existing_response.status_code}"}
            try:
                existing_payload = existing_response.json()
            except (ValueError, TypeError):
                existing_payload = {}
            existing = set(existing_payload.keys()) if isinstance(existing_payload, dict) else set()

            # Create/update streams - go2rtc will keep single connection to camera
            # and fan-out to multiple consumers (inference RTSP + WebRTC browsers)
            for name, rtsp_url in desired_map.items():
                source = _go2rtc_source_url(rtsp_url)
                # Use PUT with src param - go2rtc will create stream on demand
                # and keep it alive while consumers exist (inference worker always connected)
                try:
                    response = client.put(
                        f"{GO2RTC_API_URL}/api/streams",
                        params=[("name", name), ("src", source)],
                        timeout=5.0,
                    )
                    if response.status_code >= 400:
                        # Log but don't fail entire sync - one bad camera shouldn't break others
                        continue
                except (httpx.HTTPError, OSError):
                    continue

            # Cleanup old streams owned by this app that are no longer desired
            # Keep unrelated go2rtc streams untouched
            for name in existing - set(desired_map):
                if name.startswith(("zmk-", "cam_")):
                    try:
                        client.delete(f"{GO2RTC_API_URL}/api/streams", params=[("name", name)], timeout=3.0)
                    except (httpx.HTTPError, OSError):
                        pass
    except (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError) as exc:
        return {"ok": False, "reason": str(exc)[:220]}
    return {"ok": True, "cameras": len(desired_map), "mode": "single-connection-via-go2rtc"}

BOT_PROVIDERS=("telegram","max")

def _parse_bot_ids(value: str | None) -> list[int]:
    """Parse operator-maintained comma/newline-separated signed messenger IDs."""
    result=[]
    for token in re.split(r"[,;\s]+", str(value or "").strip()):
        if not token:
            continue
        if not re.fullmatch(r"-?\d{1,20}", token):
            raise ValueError(f"Недопустимый ID: {token}")
        parsed=int(token)
        if parsed==0:
            raise ValueError("ID не может быть нулём")
        if parsed not in result:
            result.append(parsed)
    return result


def _normalized_bot_ids(value: str | None) -> str:
    return ",".join(str(item) for item in _parse_bot_ids(value))


_TELEGRAM_USERNAME=re.compile(r"^[A-Za-z0-9_]{5,32}$")


def _normalize_telegram_username(value: str | None) -> str:
    """Return a canonical @username or an empty string for an absent handle."""
    handle=str(value or "").strip().removeprefix("@")
    if not handle:
        return ""
    if not _TELEGRAM_USERNAME.fullmatch(handle):
        raise ValueError(f"Недопустимый Telegram username: {value}")
    return "@"+handle.lower()


def _parse_telegram_principals(value: str | None) -> tuple[list[int],list[str]]:
    """Accept legacy numeric IDs and Telegram @usernames in role fields.

    Alert recipients deliberately remain numeric chat IDs: Telegram bots cannot
    reliably initiate a private message to an arbitrary username.
    """
    ids:list[int]=[]; usernames:list[str]=[]
    for token in re.split(r"[,;\s]+",str(value or "").strip()):
        if not token:
            continue
        if re.fullmatch(r"-?\d{1,20}",token):
            parsed=int(token)
            if parsed==0:
                raise ValueError("ID не может быть нулём")
            if parsed not in ids:
                ids.append(parsed)
            continue
        username=_normalize_telegram_username(token)
        if username not in usernames:
            usernames.append(username)
    return ids,usernames


def _normalized_telegram_principals(value: str | None) -> str:
    ids,usernames=_parse_telegram_principals(value)
    return ",".join([*(str(item) for item in ids),*usernames])


def _telegram_role_env(role: str) -> str:
    """Combine old *_IDS settings with readable *_USERNAMES aliases."""
    return ",".join(item for item in (os.getenv(f"TELEGRAM_{role}_IDS","").strip(),os.getenv(f"TELEGRAM_{role}_USERNAMES","").strip()) if item)


def _bot_role_for_user(provider: str, user_id: int, username: str = "") -> str:
    """Use DB-managed roles, supporting @username for Telegram only."""
    if provider not in BOT_PROVIDERS:
        return "denied"
    try:
        data={item["key"]:item["value"] for item in rows("SELECT key,value FROM settings WHERE key IN (?,?,?)",(f"{provider}_admin_ids",f"{provider}_operator_ids",f"{provider}_viewer_ids"))}
        configured=any(str(data.get(f"{provider}_{role}_ids","")).strip() for role in ("admin","operator","viewer"))
        if configured:
            if provider=="telegram":
                handle=_normalize_telegram_username(username) if username else ""
                for role in ("admin","operator","viewer"):
                    ids,usernames=_parse_telegram_principals(data.get(f"telegram_{role}_ids"))
                    if user_id in ids or handle and handle in usernames:
                        return role
            else:
                if user_id in _parse_bot_ids(data.get(f"{provider}_admin_ids")): return "admin"
                if user_id in _parse_bot_ids(data.get(f"{provider}_operator_ids")): return "operator"
                if user_id in _parse_bot_ids(data.get(f"{provider}_viewer_ids")): return "viewer"
            return "denied"
    except (sqlite3.Error,ValueError):
        pass
    legacy={"telegram":(TELEGRAM_ROLES.get(user_id) if "TELEGRAM_ROLES" in globals() else None),"max":None}
    return legacy.get(provider) or "denied"


def _active_bot_provider(settings: dict[str,str]) -> str:
    active=[provider for provider in BOT_PROVIDERS if settings.get(f"{provider}_bot_enabled")=="true"]
    return active[0] if len(active)==1 else "multiple" if active else "none"


def _managed_bot_token_dir() -> Path:
    """Return the private, persistent directory for Admin-entered bot tokens.

    Direct/local deployments default below the persistent ``data`` directory;
    Docker Compose sets a dedicated named secret volume instead.  Tokens never
    enter SQLite or a settings API response, and the volume is read-only in
    each bot worker.  Deployments can override it with ``ZMK_BOT_TOKEN_DIR``.
    """
    configured=os.getenv("ZMK_BOT_TOKEN_DIR", "").strip()
    return Path(configured) if configured else DB_PATH.parent / ".bot-tokens"


def _managed_bot_token_path(provider: str) -> Path:
    if provider not in BOT_PROVIDERS:
        raise ValueError("Неизвестный провайдер бота")
    return _managed_bot_token_dir() / f"{provider}.token"


def _read_managed_bot_token(provider: str) -> str:
    try:
        return _managed_bot_token_path(provider).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _legacy_bot_token(provider: str) -> str:
    """Read the pre-Admin .env value, retaining compatibility with old stacks."""
    if provider=="telegram":
        # The module-level value is intentionally first: security tests and
        # long-running local integrations may update it directly.
        return (TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    return os.getenv("MAX_BOT_TOKEN", "").strip()


def _effective_bot_token(provider: str) -> str:
    """Prefer the write-only Admin secret; .env remains a headless fallback."""
    if provider not in BOT_PROVIDERS:
        return ""
    return _read_managed_bot_token(provider) or _legacy_bot_token(provider)


def _bot_token_source(provider: str) -> Literal["admin","environment","none"]:
    if _read_managed_bot_token(provider):
        return "admin"
    return "environment" if _legacy_bot_token(provider) else "none"


def _bot_token_configured(provider: str) -> bool:
    return bool(_effective_bot_token(provider))


def _normalize_bot_token(value: Any) -> str:
    """Validate a write-only messenger token without ever reflecting it."""
    if not isinstance(value, str):
        raise HTTPException(422,"Токен должен быть строкой")
    token=value.strip()
    if not token:
        raise HTTPException(422,"Токен не может быть пустым")
    if len(token)>512:
        raise HTTPException(422,"Токен слишком длинный")
    if any(char.isspace() or ord(char)<32 for char in token):
        raise HTTPException(422,"Токен не должен содержать пробелы или управляющие символы")
    return token


def _store_managed_bot_token(provider: str, token: str) -> None:
    """Atomically persist a token with owner-only permissions when supported.

    A temporary sibling file plus ``replace`` means bot workers will observe
    either the old complete token or the new complete token, never a partial
    write.  The function deliberately does not log the token or include it in
    errors.
    """
    target=_managed_bot_token_path(provider)
    temporary:Path|None=None
    try:
        target.parent.mkdir(parents=True,exist_ok=True)
        try: target.parent.chmod(0o700)
        except OSError: pass  # Windows/filesystems without POSIX modes.
        with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=target.parent,prefix=f".{provider}-",delete=False) as stream:
            temporary=Path(stream.name)
            stream.write(token+"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try: temporary.chmod(0o600)
        except OSError: pass
        temporary.replace(target)
        try: target.chmod(0o600)
        except OSError: pass
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise HTTPException(503,"Не удалось сохранить токен в защищённом хранилище") from exc


def _bot_view(provider: str, settings: dict[str,str], runtime: sqlite3.Row | None, last_command: sqlite3.Row | None) -> dict[str,Any]:
    updated_at=runtime[4] if runtime else ""
    age=timestamp_age_seconds(updated_at)
    status=runtime[1] if runtime else "absent"
    return {
        "provider":provider,
        "label":"Telegram" if provider=="telegram" else "MAX",
        "enabled":settings.get(f"{provider}_bot_enabled","false")=="true",
        "alerts_enabled":settings.get(f"{provider}_alerts_enabled","false")=="true",
        "alert_min_severity":settings.get(f"{provider}_alert_min_severity","high"),
        "admin_ids":settings.get(f"{provider}_admin_ids",""),
        "operator_ids":settings.get(f"{provider}_operator_ids",""),
        "viewer_ids":settings.get(f"{provider}_viewer_ids",""),
        "alert_recipients":settings.get(f"{provider}_alert_recipients",""),
        # Never return the secret itself — only whether it exists and where it
        # was configured, so the Admin form can stay write-only.
        "token_configured":_bot_token_configured(provider),
        "token_source":_bot_token_source(provider),
        "runtime":{"status":status,"detail":runtime[2] if runtime else "Сервис ещё не сообщил состояние","enabled":bool(runtime[3]) if runtime else False,"updated_at":updated_at,"age_seconds":age,"online":bool(age is not None and age<=20 and status in {"active","disabled","waiting_token"})},
        "webapp_url":settings.get("telegram_webapp_url",os.getenv("TELEGRAM_WEBAPP_URL","")) if provider=="telegram" else "",
        "last_test":{"id":last_command[0],"status":last_command[1],"error":last_command[2],"created_at":last_command[3],"completed_at":last_command[4]} if last_command else None,
    }


def inference_worker_state() -> dict[str,Any]:
    con=db(); row=con.execute("SELECT status,detail,camera_count,updated_at,model_name,model_status,model_error FROM worker_status WHERE name='inference'").fetchone(); con.close()
    if not row: return {"connected":False,"status":"absent","detail":"Нет heartbeat от inference worker","camera_count":0,"age_seconds":None,"model_name":"","model_status":"none","model_error":""}
    age=timestamp_age_seconds(row[3]); connected=age is not None and age<=15 and row[0] in {"starting","running","idle","degraded"}
    return {"connected":connected,"status":row[0],"detail":row[1],"camera_count":row[2],"updated_at":row[3],"age_seconds":age,"model_name":row[4] or "","model_status":row[5] or "none","model_error":row[6] or ""}

def update_headers() -> dict[str,str]:
    headers = {}
    if UPDATE_TOKEN: headers["X-Update-Token"] = UPDATE_TOKEN
    return headers

def _updater_status() -> dict[str,Any]:
    if not UPDATE_SERVICE_URL:
        return {"available": False, "current":APP_VERSION, "latest":"", "update_available":False, "reason":"updater service not configured"}
    try:
        response = httpx.get(f"{UPDATE_SERVICE_URL}/status", headers=update_headers(), timeout=5)
        response.raise_for_status()
        data = response.json()
        data["available"] = True
        return data
    except httpx.HTTPError as exc:
        return {"available": False, "current":APP_VERSION, "latest":"", "update_available":False, "reason":f"updater unreachable: {type(exc).__name__}"}

@app.get("/api/update/status")
def update_status():
    return _updater_status()

@app.post("/api/update/apply")
def update_apply():
    if not UPDATE_SERVICE_URL:
        return {"status":"unavailable","message":"Сервис обновления не подключён. Запустите стенд командой ./start.sh (Linux) или .\\start.ps1 (Windows), после чего кнопка станет активной."}
    try:
        response = httpx.post(f"{UPDATE_SERVICE_URL}/apply", headers=update_headers(), timeout=120)
        if response.status_code == 400:
            return {"status":"error","message":response.json().get("detail","failed")}
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        return {"status":"error","message":f"Не удалось запустить обновление: {type(exc).__name__}"}

@app.get("/api/capabilities")
def capabilities():
    worker={"configured":bool(TRAINING_WORKER_URL),"reachable":False,"gpu":False}
    if TRAINING_WORKER_URL:
        try:
            response=httpx.get(f"{TRAINING_WORKER_URL}/health",timeout=2); response.raise_for_status(); data=response.json(); worker.update({"reachable":True,"gpu":bool(data.get("gpu")),"device":data.get("device","cpu")})
        except httpx.HTTPError: pass
    snap_dir=SNAPSHOT_DIR or (DB_PATH.parent/"snapshots")
    fresh=sum(1 for r in snap_dir.glob("*.jpg") if (time.time()-r.stat().st_mtime)<15) if snap_dir.exists() else 0
    inference=inference_worker_state()
    # A heartbeat confirms that the process is alive even before the first
    # snapshot exists; fresh frames separately confirm that a stream is live.
    return {"demo_mode":SEED_TEST_DATA,"training_worker":worker["reachable"],"training":worker,"external_inference_gateway":True,"camera_crud":True,"diagnostics":True,"search":True,"update_service":bool(UPDATE_SERVICE_URL),"inference_worker":inference["connected"],"inference":inference,"fresh_snapshots":fresh}

@app.get("/api/health")
def health(): return {"status":"ok","version":APP_VERSION,"uptime_seconds":int(time.time()-STARTED),"time":now_iso()}

@app.get("/api/dashboard")
def dashboard():
    fresh_after=(datetime.now(TZ)-timedelta(seconds=CAMERA_TELEMETRY_STALE_SECONDS)).isoformat()
    con=db(); total=con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]; online=con.execute("SELECT COUNT(*) FROM cameras WHERE status='online' AND telemetry_at>=?",(fresh_after,)).fetchone()[0]
    events24=con.execute("SELECT COUNT(*) FROM events WHERE timestamp >= ?",((datetime.now(TZ)-timedelta(days=1)).isoformat(),)).fetchone()[0]
    critical=con.execute("SELECT COUNT(*) FROM events WHERE severity='critical' AND acknowledged=0").fetchone()[0]
    avg=con.execute("SELECT COALESCE(AVG(fps),0), COALESCE(AVG(latency_ms),0) FROM cameras WHERE status='online' AND telemetry_at>=?",(fresh_after,)).fetchone()
    model=con.execute("SELECT m.name,m.precision,m.recall FROM model_registry m JOIN settings s ON s.key='active_model' AND s.value=m.name").fetchone()
    bot_settings={row[0]:row[1] for row in con.execute("SELECT key,value FROM settings WHERE key IN ('telegram_bot_enabled','max_bot_enabled')").fetchall()}
    messenger_provider=_active_bot_provider(bot_settings)
    trend=[]
    for h in range(11,-1,-1):
        end=datetime.now(TZ)-timedelta(hours=h); start=end-timedelta(hours=1)
        n=con.execute("SELECT COUNT(*) FROM events WHERE timestamp BETWEEN ? AND ?",(start.isoformat(),end.isoformat())).fetchone()[0]
        trend.append({"label":end.strftime("%H:00"),"value":n})
    con.close(); gpu=gpu_metrics(); return {"cameras":{"total":total,"online":online},"events24h":events24,"critical_unacked":critical,"avg_fps":round(avg[0],1),"avg_latency_ms":round(avg[1]),"gpu_load":gpu["gpu"],"gpu_temp":gpu["gpu_temp"],"messenger_provider":messenger_provider,"active_model":model[0] if model else None,"precision":model[1] if model else None,"recall":model[2] if model else None,"trend":trend}

EVENT_LABELS={"no_helmet":"Без каски","no_vest":"Без жилета","phone_usage":"Телефон","smoking":"Курение","restricted_zone":"Опасная зона","immobility":"Неподвижность"}
REVIEW_LABELS={"pending":"Требуют внимания","accepted":"Приняты","rejected":"Не приняты"}

def _analytics_timestamp(value:str) -> datetime|None:
    try:
        parsed=datetime.fromisoformat(value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)).astimezone(TZ)
    except (TypeError,ValueError):
        return None

def build_overview_analytics(hours:int,bucket:Literal["auto","hour","day"]="auto") -> dict[str,Any]:
    """Build real, zero-filled event analytics for the requested period."""
    now=datetime.now(TZ)
    since=now-timedelta(hours=hours)
    granularity="hour" if bucket=="hour" or (bucket=="auto" and hours<=48) else "day"
    if granularity=="hour":
        cursor=since.replace(minute=0,second=0,microsecond=0); step=timedelta(hours=1); label=lambda stamp:stamp.strftime("%d.%m %H:%M")
    else:
        cursor=since.replace(hour=0,minute=0,second=0,microsecond=0); step=timedelta(days=1); label=lambda stamp:stamp.strftime("%d.%m")
    bucket_rows:dict[str,dict[str,Any]]={}
    while cursor<=now:
        key=cursor.isoformat()
        bucket_rows[key]={"start":key,"label":label(cursor),"total":0,"critical":0,"pending":0,"accepted":0,"rejected":0,"confidence_sum":0.0}
        cursor+=step
    con=db()
    raw=con.execute("""SELECT e.timestamp,e.type,e.severity,e.confidence,e.review_status,e.acknowledged,e.camera_id,c.name camera_name,c.zone
        FROM events e LEFT JOIN cameras c ON c.id=e.camera_id WHERE e.timestamp>=? ORDER BY e.timestamp""",(since.isoformat(),)).fetchall()
    con.close()
    types:dict[str,int]={}; cameras:dict[str,dict[str,Any]]={}; review={"pending":0,"accepted":0,"rejected":0}; severity={"critical":0,"high":0,"medium":0,"low":0}
    total_confidence=0.0; total=0
    for row in raw:
        stamp=_analytics_timestamp(row[0])
        if stamp is None or stamp<since or stamp>now: continue
        key=(stamp.replace(minute=0,second=0,microsecond=0) if granularity=="hour" else stamp.replace(hour=0,minute=0,second=0,microsecond=0)).isoformat()
        slot=bucket_rows.get(key)
        status=row[4] if row[4] in REVIEW_LABELS else ("accepted" if row[5] else "pending")
        if slot:
            slot["total"]+=1; slot[status]+=1; slot["confidence_sum"]+=float(row[3])
            if row[2]=="critical": slot["critical"]+=1
        review[status]+=1; severity[row[2]]=severity.get(row[2],0)+1; types[row[1]]=types.get(row[1],0)+1
        camera=cameras.setdefault(row[6],{"camera_id":row[6],"name":row[7] or row[6],"zone":row[8] or "—","total":0,"critical":0,"pending":0,"accepted":0,"rejected":0})
        camera["total"]+=1; camera[status]+=1
        if row[2]=="critical": camera["critical"]+=1
        total+=1; total_confidence+=float(row[3])
    buckets=[]
    for slot in bucket_rows.values():
        count=slot.pop("confidence_sum")
        slot["avg_confidence"]=round(count/max(1,slot["total"]),3)
        buckets.append(slot)
    return {"generated_at":now_iso(),"hours":hours,"bucket":granularity,"period":{"from":since.isoformat(),"to":now.isoformat()},"totals":{"events":total,"critical":severity.get("critical",0),"pending":review["pending"],"accepted":review["accepted"],"rejected":review["rejected"],"avg_confidence":round(total_confidence/max(1,total),3)},"buckets":buckets,"types":[{"id":key,"label":EVENT_LABELS.get(key,key),"value":value} for key,value in sorted(types.items(),key=lambda item:(-item[1],item[0]))],"cameras":sorted(cameras.values(),key=lambda item:(-item["total"],item["name"]))[:12],"review":[{"id":key,"label":REVIEW_LABELS[key],"value":value} for key,value in review.items()],"severity":[{"id":key,"label":key.upper(),"value":value} for key,value in severity.items() if value]}

@app.get("/api/analytics/overview")
def overview_analytics(hours:int=Query(24,ge=1,le=2160),bucket:Literal["auto","hour","day"]="auto"):
    return build_overview_analytics(hours,bucket)

@app.get("/api/reports/analytics.csv")
def overview_analytics_csv(hours:int=Query(24,ge=1,le=2160),bucket:Literal["auto","hour","day"]="auto"):
    analytics=build_overview_analytics(hours,bucket)
    fields=["start","label","total","critical","pending","accepted","rejected","avg_confidence"]
    out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=fields); writer.writeheader(); writer.writerows(sanitize_csv_rows(analytics["buckets"]))
    return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",headers={"Content-Disposition":f"attachment; filename=zmk-analytics-{hours}h.csv"})

@app.get("/api/internal/cameras")
def internal_cameras(): return rows("SELECT id,name,rtsp_url,fps_limit,enabled,restart_requested_at FROM cameras WHERE enabled=1 AND rtsp_url!='' ORDER BY id")

@app.post("/api/internal/inference/heartbeat",status_code=204)
def inference_heartbeat(payload:InferenceHeartbeat):
    con=db(); con.execute("INSERT INTO worker_status(name,status,detail,camera_count,updated_at,model_name,model_status,model_error) VALUES('inference',?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET status=excluded.status,detail=excluded.detail,camera_count=excluded.camera_count,updated_at=excluded.updated_at,model_name=excluded.model_name,model_status=excluded.model_status,model_error=excluded.model_error",(payload.status,payload.detail,payload.camera_count,now_iso(),payload.model_name,payload.model_status,payload.model_error)); con.commit(); con.close()

def active_model_slots() -> dict[str,str]:
    """Return the validated specialised model mapping stored in settings."""
    try:
        raw=json.loads(_auth_setting("active_model_slots","{}"))
    except (TypeError,ValueError,json.JSONDecodeError):
        return {}
    if not isinstance(raw,dict):
        return {}
    return {role:name for role,name in raw.items() if role in MODEL_PIPELINE_ROLES and isinstance(name,str) and re.fullmatch(r"[A-Za-z0-9._-]{2,120}",name)}


def _model_test_confidence() -> float:
    try:
        return max(.01,min(.95,float(_auth_setting("model_test_conf",str(MODEL_TEST_CONF_DEFAULT)))))
    except (TypeError,ValueError):
        return MODEL_TEST_CONF_DEFAULT


def _internal_model_info(name: str, *, test_mode: bool = False) -> dict[str,Any] | None:
    data=rows("SELECT name,format,artifact_uri,checksum,source FROM model_registry WHERE name=? AND status='ready'",(name,))
    if not data:
        return None
    data[0]["test_mode"]=test_mode
    if test_mode:
        data[0]["test_conf"]=_model_test_confidence()
    return data[0]


@app.get("/api/internal/active-model")
def internal_active_model():
    active=_auth_setting("active_model","")
    # Test mode still loads and paints real boxes, but worker suppresses event
    # delivery so an unvalidated model cannot create production alerts.
    return _internal_model_info(active,test_mode=con_value("model_test_mode","false")=="true") if active else None


@app.get("/api/internal/active-models")
def internal_active_models():
    """Independent specialised models used together on each camera frame."""
    slots=[]
    for role,name in active_model_slots().items():
        info=_internal_model_info(name)
        if info:
            slots.append({"role":role,**info})
    return {"primary":internal_active_model(),"slots":slots}

import re as _re


def _valid_camera_id(camera_id:str) -> bool:
    """Camera ids are server-generated as cam_<hex>; reject anything that could
    traverse the filesystem or smuggle unsafe characters."""
    return bool(_re.fullmatch(r"[A-Za-z0-9_-]{1,64}", camera_id or ""))
def snapshot_path_for(camera_id:str) -> Path:
    base=(SNAPSHOT_DIR or (DB_PATH.parent/"snapshots"))
    if not _valid_camera_id(camera_id):
        raise HTTPException(400,"Некорректный идентификатор камеры")
    target=(base/f"{camera_id}.jpg").resolve()
    if base.resolve() != target.parent:
        raise HTTPException(400,"Недопустимый путь снимка")
    return target

def snapshot_age_seconds(camera_id:str) -> int|None:
    """Seconds since the last stored frame, or None if no frame yet."""
    target=snapshot_path_for(camera_id)
    if not target.exists(): return None
    return int(time.time()-target.stat().st_mtime)

def live_frame_age_seconds(camera_id:str) -> float|None:
    with _live_frames_lock:
        item=_live_frames.get(camera_id)
    return None if item is None else max(0,round(time.time()-item[1],2))

def clear_live_frame(camera_id:str) -> None:
    with _live_frames_lock: _live_frames.pop(camera_id,None)

def telemetry_age_seconds(value: str | None) -> int | None:
    """Return age of worker telemetry; malformed legacy values are stale."""
    return timestamp_age_seconds(value)

def camera_with_snapshot(row:dict|sqlite3.Row) -> dict:
    data=dict(row)
    data["snapshot_age_seconds"]=snapshot_age_seconds(row["id"])
    data["live_frame_age_seconds"]=live_frame_age_seconds(row["id"])
    age=telemetry_age_seconds(data.get("telemetry_at"))
    stale=data.get("status") in {"online","recovering"} and (age is None or age>CAMERA_TELEMETRY_STALE_SECONDS)
    data["telemetry_age_seconds"]=age
    data["telemetry_stale"]=stale
    if stale:
        # Never present a dead worker as a live camera solely because the last
        # row in SQLite was online. The database remains unchanged so a new
        # telemetry callback can recover it immediately.
        data["status"]="offline"; data["fps"]=0
    return data

@app.get("/api/cameras")
def cameras():
    data=rows("SELECT id,name,zone,description,fps_limit,status,fps,latency_ms,enabled,created_at,updated_at,telemetry_at,last_error,restart_requested_at,CASE WHEN rtsp_url='' THEN 0 ELSE 1 END AS configured FROM cameras ORDER BY created_at,id")
    return [camera_with_snapshot(r) for r in data]

@app.get("/api/cameras/{camera_id}")
def camera_detail(camera_id:str):
    data=rows("SELECT id,name,zone,description,fps_limit,status,fps,latency_ms,enabled,created_at,updated_at,telemetry_at,last_error,restart_requested_at,CASE WHEN rtsp_url='' THEN 0 ELSE 1 END AS configured FROM cameras WHERE id=?",(camera_id,))
    if not data: raise HTTPException(404,"Камера не найдена")
    return camera_with_snapshot(data[0])

@app.post("/api/cameras",status_code=201)
def add_camera(payload:CameraIn):
    cid=f"cam_{uuid.uuid4().hex[:12]}"; timestamp=now_iso(); con=db(); status="connecting" if payload.enabled and payload.rtsp_url else "unknown"
    con.execute("INSERT INTO cameras(id,name,zone,description,rtsp_url,fps_limit,status,fps,latency_ms,enabled,created_at,updated_at,restart_requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(cid,payload.name,payload.zone,payload.description,payload.rtsp_url,payload.fps_limit,status,0,0,int(payload.enabled),timestamp,timestamp,timestamp if status=="connecting" else "")); con.commit(); con.close()
    sync_go2rtc_cameras()
    return {"id":cid,"name":payload.name,"zone":payload.zone,"description":payload.description,"fps_limit":payload.fps_limit,"enabled":payload.enabled,"configured":bool(payload.rtsp_url),"status":status}

@app.put("/api/cameras/{camera_id}")
def update_camera(camera_id:str,payload:CameraUpdate):
    con=db(); current=con.execute("SELECT rtsp_url,enabled FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not current: con.close(); raise HTTPException(404,"Камера не найдена")
    # The RTSP URL is a secret and is never returned by the API. It is only
    # replaced when the client supplies a non-empty value; null or "" ("leave
    # as is" from the edit form) keeps the existing URL.
    new_rtsp=payload.rtsp_url if payload.rtsp_url else current[0]
    rtsp_updated=bool(payload.rtsp_url)
    stream_changed=new_rtsp != current[0] or bool(payload.enabled) != bool(current[1])
    timestamp=now_iso()
    if stream_changed:
        # A frame and telemetry from the old endpoint must not be shown as if
        # they belonged to the newly configured camera.
        con.execute("UPDATE cameras SET name=?,zone=?,description=?,rtsp_url=?,fps_limit=?,enabled=?,status='connecting',fps=0,latency_ms=0,last_error='',updated_at=?,telemetry_at='',restart_requested_at=? WHERE id=?",(payload.name,payload.zone,payload.description,new_rtsp,payload.fps_limit,int(payload.enabled),timestamp,timestamp,camera_id))
    else:
        con.execute("UPDATE cameras SET name=?,zone=?,description=?,rtsp_url=?,fps_limit=?,enabled=?,updated_at=? WHERE id=?",(payload.name,payload.zone,payload.description,new_rtsp,payload.fps_limit,int(payload.enabled),timestamp,camera_id))
    con.commit(); con.close()
    if stream_changed:
        snapshot_path_for(camera_id).unlink(missing_ok=True); clear_live_frame(camera_id)
    sync_go2rtc_cameras()
    return {"id":camera_id,"updated":True,"configured":bool(new_rtsp),"rtsp_updated":rtsp_updated,"stream_reset":stream_changed}

@app.delete("/api/cameras/{camera_id}")
def delete_camera(camera_id:str,delete_events:bool=False):
    con=db(); camera=con.execute("SELECT name FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not camera: con.close(); raise HTTPException(404,"Камера не найдена")
    event_rows=con.execute("SELECT id FROM events WHERE camera_id=?",(camera_id,)).fetchall()
    event_ids=[int(row[0]) for row in event_rows]
    event_count=len(event_ids)
    if event_count and not delete_events: con.close(); raise HTTPException(409,f"У камеры есть события: {event_count}. Подтвердите delete_events=true")
    con.execute("BEGIN IMMEDIATE")
    if delete_events: con.execute("DELETE FROM events WHERE camera_id=?",(camera_id,))
    con.execute("DELETE FROM cameras WHERE id=?",(camera_id,)); con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(now_iso(),"WARNING","camera_manager",f"Camera deleted: {camera[0]}",camera_id)); con.commit(); con.close()
    snapshot=snapshot_path_for(camera_id); snapshot.unlink(missing_ok=True); clear_live_frame(camera_id)
    if delete_events: remove_event_frames(event_ids)
    sync_go2rtc_cameras()
    return {"id":camera_id,"deleted":True,"deleted_events":event_count if delete_events else 0}

@app.patch("/api/cameras/{camera_id}/toggle")
def toggle_camera(camera_id:str):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404,"Камера не найдена")
    enabled=0 if row[0] else 1
    # Do not leave a stopped stream marked online while the worker removes it
    # from its next polling cycle.
    timestamp=now_iso(); con.execute("UPDATE cameras SET enabled=?,status=?,fps=0,latency_ms=0,last_error='',updated_at=?,telemetry_at='',restart_requested_at=? WHERE id=?",(enabled,"connecting" if enabled else "unknown",timestamp,timestamp,camera_id)); con.commit(); con.close()
    snapshot_path_for(camera_id).unlink(missing_ok=True); clear_live_frame(camera_id)
    sync_go2rtc_cameras()
    return {"id":camera_id,"enabled":bool(enabled),"stream_reset":True}

@app.post("/api/cameras/{camera_id}/restart")
def restart_camera(camera_id:str):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404,"Камера не найдена")
    if not row[0]: con.close(); raise HTTPException(409,"Сначала включите аналитику камеры")
    timestamp=now_iso(); con.execute("UPDATE cameras SET status='connecting',fps=0,latency_ms=0,last_error='',telemetry_at='',restart_requested_at=?,updated_at=? WHERE id=?",(timestamp,timestamp,camera_id)); con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(timestamp,"INFO","camera_manager","RTSP restart requested",camera_id)); con.commit(); con.close()
    snapshot_path_for(camera_id).unlink(missing_ok=True); clear_live_frame(camera_id)
    sync_go2rtc_cameras()
    return {"id":camera_id,"restart_requested_at":timestamp,"status":"connecting"}

def apply_camera_telemetry(camera_id:str,payload:CameraTelemetry):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404,"Камера не найдена")
    if not row[0]:
        con.close()
        # An in-flight worker frame must not resurrect a disabled camera.
        return {"id":camera_id,"ignored":True,"status":"unknown","fps":0,"latency_ms":0,"error":""}
    timestamp=now_iso(); error=safe_camera_error(payload.error); con.execute("UPDATE cameras SET status=?,fps=?,latency_ms=?,last_error=?,updated_at=?,telemetry_at=? WHERE id=?",(payload.status,payload.fps,payload.latency_ms,error,timestamp,timestamp,camera_id)); con.commit(); con.close()
    return {"id":camera_id,**payload.model_dump()}

@app.post("/api/cameras/{camera_id}/telemetry")
def camera_telemetry(camera_id:str,payload:CameraTelemetry):
    return apply_camera_telemetry(camera_id,payload)

@app.post("/api/internal/cameras/{camera_id}/telemetry")
def internal_camera_telemetry(camera_id:str,payload:CameraTelemetry):
    return apply_camera_telemetry(camera_id,payload)

def store_camera_snapshot(camera_id:str,payload:CameraSnapshotIn):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Камера не найдена")
    if not row[0]: return
    try: image=base64.b64decode(payload.jpeg_base64,validate=True)
    except (binascii.Error,ValueError): raise HTTPException(422,"Некорректный base64 JPEG")
    if len(image)>1_300_000 or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"): raise HTTPException(422,"Некорректный или слишком большой JPEG")
    target=snapshot_path_for(camera_id); target.parent.mkdir(parents=True,exist_ok=True); temp=target.with_name(f".{target.stem}.tmp"); temp.write_bytes(image); temp.replace(target)

@app.post("/api/cameras/{camera_id}/snapshot",status_code=204)
def upload_camera_snapshot(camera_id:str,payload:CameraSnapshotIn):
    store_camera_snapshot(camera_id,payload)

@app.post("/api/internal/cameras/{camera_id}/snapshot",status_code=204)
def internal_camera_snapshot(camera_id:str,payload:CameraSnapshotIn):
    store_camera_snapshot(camera_id,payload)

@app.post("/api/internal/cameras/{camera_id}/live-frame",status_code=204)
async def internal_live_frame(camera_id:str,request:Request):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Камера не найдена")
    if not row[0]: return
    image=await request.body()
    if len(image)>1_300_000 or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
        raise HTTPException(422,"Некорректный или слишком большой live JPEG")
    global _live_frame_sequence
    with _live_frames_lock:
        _live_frame_sequence+=1
        _live_frames[camera_id]=(_live_frame_sequence,time.time(),image)

@app.post("/api/internal/cameras/{camera_id}/visual",status_code=204)
async def internal_live_visual(camera_id:str,request:Request):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Камера не найдена")
    if not row[0]: return
    try:
        payload=await request.json()
    except (ValueError,TypeError):
        raise HTTPException(422,"Некорректный visual JSON")
    # payload should contain shape and boxes
    if not isinstance(payload, dict): raise HTTPException(422,"Некорректный visual")
    with _live_visuals_lock:
        _live_visuals[camera_id]=(time.time(), payload)

@app.get("/api/cameras/{camera_id}/visual")
def camera_visual(camera_id:str):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Камера не найдена")
    if not row[0]: raise HTTPException(409,"Аналитика камеры отключена")
    with _live_visuals_lock:
        item=_live_visuals.get(camera_id)
    if not item: return {"camera_id":camera_id,"timestamp":None,"shape":None,"boxes":[],"age_seconds":None}
    ts, payload = item
    age = round(time.time()-ts,2)
    # Expire old visuals after 5 seconds of no inference
    if age>5:
        return {"camera_id":camera_id,"timestamp":None,"shape":None,"boxes":[],"age_seconds":age}
    result=dict(payload)
    result["camera_id"]=camera_id
    result["age_seconds"]=age
    return result

@app.post("/api/internal/events/{event_id}/frame",status_code=204)
async def internal_event_frame(event_id:int,request:Request):
    """Persist the annotated frame that produced an accepted event."""
    con=db(); row=con.execute("SELECT id FROM events WHERE id=?",(event_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Событие не найдено")
    image=await request.body()
    if len(image)>1_300_000 or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
        raise HTTPException(422,"Некорректный или слишком большой JPEG кадра события")
    target=event_frame_path_for(event_id); target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_name(f".{target.stem}-{uuid.uuid4().hex}.tmp")
    temp.write_bytes(image); temp.replace(target)

@app.get("/api/cameras/{camera_id}/mjpeg")
def camera_mjpeg(camera_id:str):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Камера не найдена")
    if not row[0]: raise HTTPException(409,"Аналитика камеры отключена")
    # v2.15.0: WebRTC H264 is primary like VLC, but this MJPEG is reliable fallback.
    # Never return 503 black screen - always try go2rtc, then live frames, then snapshot loop.
    # Frontend now has JPEG polling too, so black screen impossible.
    if GO2RTC_ENABLED and GO2RTC_API_URL:
        for candidate_name in (f"zmk-{camera_id}", camera_id):
            try:
                with httpx.Client(timeout=httpx.Timeout(3.0, connect=1.5)) as client:
                    chk = client.get(f"{GO2RTC_API_URL}/api/frame.jpeg", params={"src": candidate_name}, timeout=1.5)
                    if chk.status_code==200 and chk.content.startswith(b"\xff\xd8"):
                        def go2rtc_generate(src_name: str = candidate_name):
                            try:
                                with httpx.stream("GET", f"{GO2RTC_API_URL}/api/stream.mjpeg", params={"src": src_name}, timeout=httpx.Timeout(60.0, connect=2.0)) as r:
                                    for chunk in r.iter_bytes(chunk_size=8192):
                                        if chunk:
                                            yield chunk
                            except (httpx.HTTPError, OSError, RuntimeError, ValueError):
                                return
                        return StreamingResponse(go2rtc_generate(), media_type="multipart/x-mixed-replace; boundary=--go2rtc", headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})
            except (httpx.HTTPError, OSError, RuntimeError, ValueError):
                continue
    # Fallback to worker live frames (now kept even when go2rtc enabled at 10 FPS for reliability)
    def generate():
        sequence=-1
        idle=0
        while True:
            with _live_frames_lock:
                item=_live_frames.get(camera_id)
            if item and item[0]!=sequence:
                sequence,_,image=item
                idle=0
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "+str(len(image)).encode()+b"\r\n\r\n"+image+b"\r\n"
            else:
                idle+=1
                # If no live frame for 3 sec, try serving snapshot as MJPEG to avoid black screen
                if idle>150:  # 150 * 0.02 = 3 sec
                    try:
                        snap_path = snapshot_path_for(camera_id)
                        if snap_path.exists():
                            img = snap_path.read_bytes()
                            if img.startswith(b"\xff\xd8"):
                                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "+str(len(img)).encode()+b"\r\n\r\n"+img+b"\r\n"
                                idle=0
                                time.sleep(0.5)
                                continue
                    except Exception:
                        pass
                    # If still no frame, wait and continue loop (don't break - keep connection for snapshot fallback)
                    if idle>500:  # 10 sec total, then reset idle to keep trying
                        idle=0
            time.sleep(.02)
    return StreamingResponse(generate(),media_type="multipart/x-mixed-replace; boundary=frame",headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

@app.get("/api/cameras/{camera_id}/snapshot")
def camera_snapshot(camera_id:str):
    target=snapshot_path_for(camera_id)
    if not target.exists(): raise HTTPException(404,"Кадр ещё не получен")
    return FileResponse(target,media_type="image/jpeg",headers={"Cache-Control":"no-store"})

def diagnose_camera_row(camera_id:str):
    con=db(); row=con.execute("SELECT id,name,rtsp_url,enabled,status,telemetry_at,last_error,restart_requested_at FROM cameras WHERE id=?",(camera_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Камера не найдена")
    runtime={"camera_status":row[4],"telemetry_age_seconds":telemetry_age_seconds(row[5]),"last_error":row[6],"restart_requested_at":row[7]}
    if not row[3]: return {"camera_id":camera_id,"name":row[1],"reachable":False,"status":"disabled","latency_ms":None,"message":"Аналитика камеры отключена",**runtime}
    if not row[2]: return {"camera_id":camera_id,"name":row[1],"reachable":False,"status":"not_configured","latency_ms":None,"message":"RTSP URL не задан",**runtime}
    try:
        parsed=urlparse(row[2]); host=parsed.hostname; port=parsed.port or (322 if parsed.scheme=="rtsps" else 554)
    except ValueError:
        return {"camera_id":camera_id,"name":row[1],"reachable":False,"status":"invalid_url","latency_ms":None,"message":"Некорректный RTSP URL",**runtime}
    if not host: return {"camera_id":camera_id,"name":row[1],"reachable":False,"status":"invalid_url","latency_ms":None,"message":"Некорректный RTSP URL",**runtime}
    started=time.perf_counter()
    try:
        with socket.create_connection((host,port),timeout=3): pass
        latency=round((time.perf_counter()-started)*1000)
        return {"camera_id":camera_id,"name":row[1],"reachable":True,"status":"reachable","latency_ms":latency,"message":"TCP-подключение установлено",**runtime}
    except OSError as exc:
        return {"camera_id":camera_id,"name":row[1],"reachable":False,"status":"unreachable","latency_ms":None,"message":str(exc)[:200],**runtime}

@app.post("/api/cameras/{camera_id}/diagnostics")
def diagnose_camera(camera_id:str):
    return diagnose_camera_row(camera_id)

@app.get("/api/diagnostics")
def diagnostics():
    camera_ids=[r["id"] for r in rows("SELECT id FROM cameras ORDER BY id")]
    # Snapshot freshness is the state when the operator starts diagnostics, not
    # after a group of slow TCP checks has consumed the freshness window.
    snapshot_state={}
    for camera_id in camera_ids:
        age=snapshot_age_seconds(camera_id)
        snapshot_state[camera_id]={"snapshot_age_seconds":age,"snapshot":"fresh" if age is not None and age<15 else ("stale" if age is not None else "none"),"live_frame_age_seconds":live_frame_age_seconds(camera_id)}
    with ThreadPoolExecutor(max_workers=min(10,max(1,len(camera_ids)))) as pool: camera_results=list(pool.map(diagnose_camera_row,camera_ids))
    for result in camera_results:
        result.update(snapshot_state.get(result["camera_id"],{"snapshot_age_seconds":None,"snapshot":"none","live_frame_age_seconds":None}))
    return {"generated_at":now_iso(),"system":system_health_data(),"worker":inference_worker_state(),"cameras":camera_results}

@app.get("/api/events")
def events(limit:int=Query(50,ge=1,le=500),severity:str|None=None,event_type:str|None=None,acknowledged:bool|None=None,review_status:Literal["pending","accepted","rejected"]|None=None):
    ack=int(acknowledged) if acknowledged is not None else None
    data=rows("""SELECT e.*,c.name camera_name,c.zone FROM events e JOIN cameras c ON c.id=e.camera_id
        WHERE (? IS NULL OR e.severity=?) AND (? IS NULL OR e.type=?) AND (? IS NULL OR e.acknowledged=?) AND (? IS NULL OR e.review_status=?)
        ORDER BY e.timestamp DESC LIMIT ?""",(severity,severity,event_type,event_type,ack,ack,review_status,review_status,limit))
    for item in data: item["has_frame"]=event_frame_path_for(int(item["id"])).is_file()
    return data

@app.get("/api/events/by-id/{event_id}")
def event_by_id(event_id:int):
    data=rows("""SELECT e.*,c.name camera_name,c.zone FROM events e JOIN cameras c ON c.id=e.camera_id
        WHERE e.id=?""",(event_id,))
    if not data: raise HTTPException(404,"Событие не найдено")
    item=data[0]; item["has_frame"]=event_frame_path_for(event_id).is_file()
    return item

@app.get("/api/events/{event_id}/frame")
def event_frame(event_id:int):
    con=db(); row=con.execute("SELECT id FROM events WHERE id=?",(event_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Событие не найдено")
    target=event_frame_path_for(event_id)
    if not target.is_file(): raise HTTPException(404,"Кадр события ещё не сохранён")
    return FileResponse(target,media_type="image/jpeg",headers={"Cache-Control":"no-store"})

def _bulk_review_events(payload:BulkAckIn,review_status:Literal["accepted","rejected"],default_note:str) -> dict:
    """Apply one review decision to a validated group of event IDs."""
    event_ids=payload.event_ids
    marks=",".join("?" for _ in event_ids)
    note=payload.note.strip() or default_note
    con=db()
    try:
        rows_found=con.execute(f"SELECT id,review_status FROM events WHERE id IN ({marks})",event_ids).fetchall()  # nosec B608 - placeholders only
        found={int(row[0]):str(row[1] or "pending") for row in rows_found}
        missing=[event_id for event_id in event_ids if event_id not in found]
        updated=[event_id for event_id in event_ids if found.get(event_id)!=review_status]
        already=[event_id for event_id in event_ids if found.get(event_id)==review_status]
        if updated:
            update_marks=",".join("?" for _ in updated)
            con.execute(f"UPDATE events SET acknowledged=1,review_status=?,reviewed_at=?,note=? WHERE id IN ({update_marks})",(review_status,now_iso(),note,*updated))  # nosec B608 - placeholders only
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","event_manager",f"Bulk review status={review_status} updated={len(updated)} already={len(already)} missing={len(missing)}"))
        con.commit()
        return {"updated_ids":updated,"already_ids":already,"missing_ids":missing,"review_status":review_status,"note":note}
    finally:
        con.close()

@app.post("/api/events/ack-bulk")
def ack_events_bulk(payload:BulkAckIn):
    result=_bulk_review_events(payload,"accepted","Проверено оператором")
    return {**result,"acknowledged_ids":result["updated_ids"],"already_acknowledged_ids":result["already_ids"]}

@app.post("/api/events/reject-bulk")
def reject_events_bulk(payload:BulkAckIn):
    result=_bulk_review_events(payload,"rejected","Не принято оператором")
    return {**result,"rejected_ids":result["updated_ids"],"already_rejected_ids":result["already_ids"]}

@app.post("/api/events/{event_id}/ack")
def ack(event_id:int,payload:AckIn):
    note=payload.note.strip() or "Проверено оператором"
    con=db(); cur=con.execute("UPDATE events SET acknowledged=1,review_status='accepted',reviewed_at=?,note=? WHERE id=?",(now_iso(),note,event_id)); con.commit(); con.close()
    if not cur.rowcount: raise HTTPException(404,"Событие не найдено")
    return {"id":event_id,"acknowledged":True,"review_status":"accepted","note":note}

@app.post("/api/events/{event_id}/reject")
def reject_event(event_id:int,payload:AckIn):
    note=payload.note.strip() or "Не принято оператором"
    con=db(); cur=con.execute("UPDATE events SET acknowledged=1,review_status='rejected',reviewed_at=?,note=? WHERE id=?",(now_iso(),note,event_id)); con.commit(); con.close()
    if not cur.rowcount: raise HTTPException(404,"Событие не найдено")
    return {"id":event_id,"acknowledged":True,"review_status":"rejected","note":note}
@app.post("/api/inference/detections")
def ingest_detections(payload:DetectionBatch):
    """Validated contract from inference workers to the event subsystem."""
    con=db(); con.execute("BEGIN IMMEDIATE"); active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
    slot_models=set(active_model_slots().values())
    active_models={str(active)} if active else set()
    active_models.update(slot_models)
    thresholds={"no_helmet":"helmet_conf","no_vest":"vest_conf","phone_usage":"phone_conf","smoking":"smoking_conf","restricted_zone":"restricted_zone_conf","immobility":"immobility_conf"}
    cooldown_row=con.execute("SELECT value FROM settings WHERE key='event_cooldown_seconds'").fetchone()
    try: cooldown=max(0,int(float(cooldown_row[0]))) if cooldown_row else 30
    except ValueError: cooldown=30
    accepted=[]; rejected=[]
    for i,d in enumerate(payload.detections):
        cam=con.execute("SELECT status,enabled,telemetry_at FROM cameras WHERE id=?",(d.camera_id,)).fetchone()
        cam_age=telemetry_age_seconds(cam[2]) if cam else None
        reason=None; normalized_timestamp=now_iso()
        if d.detection_id:
            existing=con.execute("SELECT id FROM events WHERE external_id=?",(d.detection_id,)).fetchone()
            if existing: accepted.append({"index":i,"event_id":existing[0],"duplicate":True}); continue
        if d.timestamp:
            event_time=(d.timestamp if d.timestamp.tzinfo else d.timestamp.replace(tzinfo=TZ)).astimezone(TZ)
            normalized_timestamp=event_time.isoformat()
            now=datetime.now(TZ)
            if event_time>now+timedelta(minutes=10) or event_time<now-timedelta(days=7): reason="timestamp_out_of_range"
        if not reason:
            if d.model_name not in active_models: reason=f"stale_model: active={','.join(sorted(active_models)) or 'none'}"
            elif not cam: reason="unknown_camera"
            elif cam[0] != "online" or not cam[1] or cam_age is None or cam_age>CAMERA_TELEMETRY_STALE_SECONDS: reason="camera_unavailable"
            else:
                key=thresholds[d.event_type]; threshold=float(con.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()[0])
                if d.confidence < threshold: reason=f"below_threshold:{threshold}"
        if not reason and cooldown>0:
            cutoff=(datetime.now(TZ)-timedelta(seconds=cooldown)).isoformat()
            recent=con.execute("SELECT id FROM events WHERE camera_id=? AND type=? AND person_id IS ? AND timestamp>=? ORDER BY id DESC LIMIT 1",(d.camera_id,d.event_type,d.person_id,cutoff)).fetchone()
            if recent: rejected.append({"index":i,"reason":f"event_cooldown:{cooldown}","event_id":recent[0]}); continue
        if reason: rejected.append({"index":i,"reason":reason}); continue
        severity="critical" if d.event_type in {"restricted_zone","immobility"} else "high" if d.event_type in {"no_helmet","smoking"} else "medium"
        cur=con.execute("INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,external_id) VALUES(?,?,?,?,?,?,?)",(normalized_timestamp,d.camera_id,d.event_type,severity,d.confidence,d.person_id,d.detection_id))
        accepted.append({"index":i,"event_id":cur.lastrowid})
    con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","inference_gateway",f"batch models={','.join(sorted(active_models)) or 'none'} accepted={len(accepted)} rejected={len(rejected)}"))
    webhook={r[0]:r[1] for r in con.execute("SELECT key,value FROM settings WHERE key IN ('webhook_enabled','webhook_url','webhook_timeout')").fetchall()}; con.commit(); con.close()
    if accepted and webhook.get('webhook_enabled')=='true' and webhook.get('webhook_url'):
        try: httpx.post(webhook['webhook_url'],json={"source":"zmk-vision","model":active or None,"models":sorted(active_models),"events":accepted,"timestamp":now_iso()},timeout=float(webhook.get('webhook_timeout','5'))).raise_for_status()
        except httpx.HTTPError as exc:
            logcon=db(); logcon.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"ERROR","integration",f"Webhook delivery failed: {str(exc)[:300]}")); logcon.commit(); logcon.close()
    return {"active_model":active,"active_models":sorted(active_models),"accepted":accepted,"rejected":rejected,"received":len(payload.detections)}

@app.get("/api/admin/config")
def get_config():
    data={r["key"]:r["value"] for r in rows("SELECT * FROM settings")}
    groups={
      "general":["site_name","timezone","language","retention_days"],
      "inference":["inference_fps","inference_device","batch_size","nms_iou","model_test_conf","helmet_conf","vest_conf","phone_conf","smoking_conf","restricted_zone_conf","immobility_conf","min_model_precision","min_model_recall","event_cooldown_seconds"],
      "archive":["archive_quality","archive_clip_seconds","minio_endpoint","minio_bucket","minio_secure"],
      "notifications":["telegram_enabled","telegram_chat_ids","critical_alerts"],
      "integration":["webhook_enabled","webhook_url","webhook_timeout","rtsp_reconnect_seconds"]}
    return {g:{k:data.get(k,"") for k in keys} for g,keys in groups.items()}

CONFIG_ALLOWED={"site_name","timezone","language","retention_days","inference_fps","inference_device","batch_size","nms_iou","model_test_conf","helmet_conf","vest_conf","phone_conf","smoking_conf","restricted_zone_conf","immobility_conf","min_model_precision","min_model_recall","event_cooldown_seconds","archive_quality","archive_clip_seconds","minio_endpoint","minio_bucket","minio_secure","telegram_enabled","telegram_chat_ids","critical_alerts","webhook_enabled","webhook_url","webhook_timeout","rtsp_reconnect_seconds"}
@app.put("/api/admin/config")
def update_config(payload:ConfigPatch):
    unknown=set(payload.values)-CONFIG_ALLOWED
    if unknown: raise HTTPException(422,f"Неизвестные параметры: {', '.join(sorted(unknown))}")
    numeric={"retention_days":(1,3650),"inference_fps":(1,30),"batch_size":(1,64),"nms_iou":(.1,.95),"model_test_conf":(.01,.95),"helmet_conf":(.1,1),"vest_conf":(.1,1),"phone_conf":(.1,1),"smoking_conf":(.1,1),"restricted_zone_conf":(.1,1),"immobility_conf":(.1,1),"min_model_precision":(0,100),"min_model_recall":(0,100),"event_cooldown_seconds":(0,3600),"archive_quality":(10,100),"archive_clip_seconds":(2,120),"webhook_timeout":(1,60),"rtsp_reconnect_seconds":(1,300)}
    for key,(lo,hi) in numeric.items():
        if key in payload.values:
            try: value=float(payload.values[key])
            except (TypeError,ValueError): raise HTTPException(422,f"{key}: требуется число")
            if not lo<=value<=hi: raise HTTPException(422,f"{key}: допустимо {lo}..{hi}")
    current={r["key"]:r["value"] for r in rows("SELECT * FROM settings")}; merged={**current,**{k:str(v).lower() if isinstance(v,bool) else str(v) for k,v in payload.values.items()}}
    if merged.get("webhook_enabled")=="true" and not merged.get("webhook_url","").startswith(("http://","https://")): raise HTTPException(422,"Для включения webhook укажите корректный HTTP(S) URL")
    if merged.get("telegram_enabled")=="true" and not merged.get("telegram_chat_ids","").strip(): raise HTTPException(422,"Для Telegram укажите хотя бы один chat ID")
    if not merged.get("minio_endpoint","").strip() or not merged.get("minio_bucket","").strip(): raise HTTPException(422,"MinIO endpoint и bucket обязательны")
    con=db();
    for key,value in payload.values.items(): con.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value).lower() if isinstance(value,bool) else str(value)))
    con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","admin",f"Configuration updated: {', '.join(payload.values.keys())}"))
    if "retention_days" in payload.values: apply_retention(con)
    con.commit(); con.close()
    return {"updated":list(payload.values),"restart_required":any(k in payload.values for k in ("inference_device","minio_endpoint"))}

def _bot_settings_map(con: sqlite3.Connection) -> dict[str,str]:
    keys=[f"{provider}_{field}" for provider in BOT_PROVIDERS for field in ("bot_enabled","alerts_enabled","alert_min_severity","admin_ids","operator_ids","viewer_ids","alert_recipients","webapp_url")]
    marks=",".join("?" for _ in keys)
    return {row[0]:row[1] for row in con.execute(f"SELECT key,value FROM settings WHERE key IN ({marks})",keys).fetchall()}  # nosec B608 - placeholders only


@app.get("/api/admin/bots")
def admin_bots():
    con=db()
    try:
        settings=_bot_settings_map(con)
        providers=[]
        for provider in BOT_PROVIDERS:
            runtime=con.execute("SELECT provider,status,detail,enabled,updated_at FROM bot_runtime WHERE provider=?",(provider,)).fetchone()
            last_command=con.execute("SELECT id,status,error,created_at,completed_at FROM bot_commands WHERE provider=? AND action='test_alert' ORDER BY id DESC LIMIT 1",(provider,)).fetchone()
            providers.append(_bot_view(provider,settings,runtime,last_command))
        return {"providers":providers,"active_providers":[provider for provider in BOT_PROVIDERS if settings.get(f"{provider}_bot_enabled")=="true"]}
    finally:
        con.close()


@app.put("/api/admin/bots/{provider}")
def update_bot(provider: Literal["telegram","max"], payload: BotConfigIn):
    try:
        values={
            f"{provider}_bot_enabled":"true" if payload.enabled else "false",
            f"{provider}_alerts_enabled":"true" if payload.alerts_enabled else "false",
            f"{provider}_alert_min_severity":payload.alert_min_severity,
            f"{provider}_admin_ids":(_normalized_telegram_principals(payload.admin_ids) if provider=="telegram" else _normalized_bot_ids(payload.admin_ids)),
            f"{provider}_operator_ids":(_normalized_telegram_principals(payload.operator_ids) if provider=="telegram" else _normalized_bot_ids(payload.operator_ids)),
            f"{provider}_viewer_ids":(_normalized_telegram_principals(payload.viewer_ids) if provider=="telegram" else _normalized_bot_ids(payload.viewer_ids)),
            f"{provider}_alert_recipients":_normalized_bot_ids(payload.alert_recipients),
        }
        if provider=="telegram":
            url=payload.webapp_url.strip()
            if url and not url.startswith(("https://","http://localhost")):
                raise HTTPException(422,"Telegram Mini App URL должен быть HTTPS (localhost разрешён только для локальной разработки)")
            values["telegram_webapp_url"]=url
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    # ``token`` is optional: an empty field in the browser must not erase an
    # existing secret.  A non-empty value atomically replaces it before the
    # enablement check, allowing operators to add a token and enable a bot with
    # one Save action.
    token=_normalize_bot_token(payload.token) if payload.token is not None else ""
    if payload.enabled and not (token or _bot_token_configured(provider)):
        label="Telegram" if provider=="telegram" else "MAX"
        raise HTTPException(422,f"Нельзя включить {label}: сначала введите токен в Admin → Боты или задайте его в .env")
    if token:
        _store_managed_bot_token(provider,token)
    con=db()
    try:
        for key,value in values.items(): con.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
        # Keep legacy settings coherent for existing integrations and upgrades.
        if provider=="telegram":
            con.execute("INSERT INTO settings VALUES('telegram_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(values[f"{provider}_alerts_enabled"],))
            con.execute("INSERT INTO settings VALUES('telegram_chat_ids',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(values[f"{provider}_alert_recipients"],))
            con.execute("INSERT INTO settings VALUES('critical_alerts',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("true" if payload.alert_min_severity=="critical" else "false",))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","bot_admin",f"{provider} bot configured: enabled={payload.enabled}, alerts={payload.alerts_enabled}, token_updated={bool(token)}"))
        con.commit()
        runtime=con.execute("SELECT provider,status,detail,enabled,updated_at FROM bot_runtime WHERE provider=?",(provider,)).fetchone()
        last_command=con.execute("SELECT id,status,error,created_at,completed_at FROM bot_commands WHERE provider=? AND action='test_alert' ORDER BY id DESC LIMIT 1",(provider,)).fetchone()
        settings=_bot_settings_map(con)
        return _bot_view(provider,settings,runtime,last_command)
    finally:
        con.close()


@app.post("/api/admin/bots/{provider}/test-alert",status_code=202)
def request_bot_test_alert(provider: Literal["telegram","max"]):
    if not _bot_token_configured(provider):
        raise HTTPException(422,"У этого бота нет токена на сервере")
    con=db()
    try:
        settings=_bot_settings_map(con)
        if settings.get(f"{provider}_bot_enabled")!="true":
            raise HTTPException(409,"Сначала включите бота и сохраните настройки")
        cur=con.execute("INSERT INTO bot_commands(provider,action,payload,status,created_at) VALUES(?,?,?,?,?)",(provider,"test_alert",json.dumps({"text":"Тестовое сообщение из Admin панели ZMK Vision"},ensure_ascii=False),"pending",now_iso()))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","bot_admin",f"{provider} test alert queued: command={cur.lastrowid}"))
        con.commit()
        return {"id":cur.lastrowid,"provider":provider,"status":"pending","message":"Тест поставлен в очередь бота"}
    finally:
        con.close()


@app.get("/api/bots/{provider}/runtime")
def bot_runtime_config(provider: Literal["telegram","max"]):
    con=db()
    try:
        settings=_bot_settings_map(con)
        if provider=="telegram":
            admin_ids,admin_usernames=_parse_telegram_principals(settings.get("telegram_admin_ids"))
            operator_ids,operator_usernames=_parse_telegram_principals(settings.get("telegram_operator_ids"))
            viewer_ids,viewer_usernames=_parse_telegram_principals(settings.get("telegram_viewer_ids"))
        else:
            admin_ids,operator_ids,viewer_ids=(_parse_bot_ids(settings.get("max_admin_ids")),_parse_bot_ids(settings.get("max_operator_ids")),_parse_bot_ids(settings.get("max_viewer_ids")))
            admin_usernames,operator_usernames,viewer_usernames=[],[],[]
        return {
            "provider":provider,
            "enabled":settings.get(f"{provider}_bot_enabled","false")=="true",
            "alerts_enabled":settings.get(f"{provider}_alerts_enabled","false")=="true",
            "alert_min_severity":settings.get(f"{provider}_alert_min_severity","high"),
            "admin_ids":admin_ids,
            "operator_ids":operator_ids,
            "viewer_ids":viewer_ids,
            "admin_usernames":admin_usernames,
            "operator_usernames":operator_usernames,
            "viewer_usernames":viewer_usernames,
            "alert_recipients":_parse_bot_ids(settings.get(f"{provider}_alert_recipients")),
            "webapp_url":settings.get("telegram_webapp_url","") if provider=="telegram" else "",
        }
    finally:
        con.close()


@app.post("/api/bots/{provider}/heartbeat")
def bot_heartbeat(provider: Literal["telegram","max"], payload: BotHeartbeatIn):
    con=db(); con.execute("INSERT INTO bot_runtime(provider,status,detail,enabled,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET status=excluded.status,detail=excluded.detail,enabled=excluded.enabled,updated_at=excluded.updated_at",(provider,payload.status,payload.detail,payload.enabled,now_iso())); con.commit(); con.close()
    return {"provider":provider,"status":payload.status}


@app.get("/api/bots/{provider}/commands")
def bot_commands(provider: Literal["telegram","max"], limit:int=Query(10,ge=1,le=50)):
    con=db()
    try:
        result=[]
        for row in con.execute("SELECT id,action,payload,created_at FROM bot_commands WHERE provider=? AND status='pending' ORDER BY id LIMIT ?",(provider,limit)).fetchall():
            try: payload=json.loads(row[2])
            except (TypeError,ValueError,json.JSONDecodeError): payload={}
            result.append({"id":row[0],"action":row[1],"payload":payload,"created_at":row[3]})
        return {"commands":result}
    finally:
        con.close()


@app.post("/api/bots/{provider}/commands/{command_id}/complete")
def complete_bot_command(provider: Literal["telegram","max"], command_id:int, payload: BotCommandCompleteIn):
    con=db()
    try:
        row=con.execute("SELECT id FROM bot_commands WHERE id=? AND provider=? AND status='pending'",(command_id,provider)).fetchone()
        if not row: raise HTTPException(404,"Команда бота не найдена или уже завершена")
        con.execute("UPDATE bot_commands SET status=?,error=?,completed_at=? WHERE id=?",(payload.status,payload.error,now_iso(),command_id))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO" if payload.status=="completed" else "ERROR","bot_admin",f"{provider} command {command_id}: {payload.status}"))
        con.commit(); return {"id":command_id,"status":payload.status}
    finally:
        con.close()


@app.get("/api/admin/users")
def get_users(): return rows("SELECT id,name,login,role,active,created_at FROM users ORDER BY id")
@app.post("/api/admin/users",status_code=201)
def create_user(payload:UserIn):
    con=db()
    try: cur=con.execute("INSERT INTO users(name,login,role,active,created_at) VALUES(?,?,?,?,?)",(payload.name,payload.login,payload.role,1,now_iso())); con.commit()
    except sqlite3.IntegrityError: con.close(); raise HTTPException(409,"Логин уже используется")
    uid=cur.lastrowid; con.close(); return {"id":uid,**payload.model_dump(),"active":True}
@app.patch("/api/admin/users/{user_id}/toggle")
def toggle_user(user_id:int):
    con=db(); row=con.execute("SELECT active,role FROM users WHERE id=?",(user_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404,"Пользователь не найден")
    if row[1]=="admin" and row[0]:
        admins=con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
        if admins<=1: con.close(); raise HTTPException(409,"Нельзя отключить последнего администратора")
    active=0 if row[0] else 1; con.execute("UPDATE users SET active=? WHERE id=?",(active,user_id)); con.commit(); con.close(); return {"id":user_id,"active":bool(active)}

@app.get("/api/logs")
def logs(level:str|None=None,limit:int=Query(100,ge=1,le=500)):
    return rows("SELECT * FROM logs WHERE (? IS NULL OR level=?) ORDER BY id DESC LIMIT ?",(level,level,limit))
@app.get("/api/settings")
def settings(): return {r["key"]:r["value"] for r in rows("SELECT * FROM settings")}
@app.put("/api/settings/{key}")
def update_setting(key:Literal["helmet_conf","vest_conf","phone_conf","smoking_conf","restricted_zone_conf","immobility_conf"],payload:SettingIn):
    con=db(); con.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(payload.value))); con.commit(); con.close(); return {"key":key,"value":payload.value}
def _trial_preset_ids() -> set[str]:
    """IDs allowed to run only after an explicit trial-mode request."""
    return {str(p.get("id")) for p in MODEL_PRESETS if p.get("trial_activation")}


def _is_trial_preset_source(source: str | None) -> bool:
    return bool(source and source.startswith("preset:") and source.removeprefix("preset:") in _trial_preset_ids())


def _model_meets_quality(precision: float | None, recall: float | None, limits: dict[str, float]) -> bool:
    return bool(
        precision is not None
        and recall is not None
        and precision >= limits.get("min_model_precision", 90)
        and recall >= limits.get("min_model_recall", 85)
    )


def _model_runtime_view(name: str, active: bool, worker: dict[str,Any]) -> dict[str,Any]:
    """Expose whether the selected model has really reached inference worker."""
    base={"worker_connected":bool(worker.get("connected")),"worker_updated_at":worker.get("updated_at",""),"worker_age_seconds":worker.get("age_seconds")}
    if not active:
        return {**base,"status":"inactive","detail":""}
    if not worker.get("connected"):
        return {**base,"status":"worker_offline","detail":"Inference worker не на связи — запустите профиль inference"}
    worker_model=str(worker.get("model_name") or "")
    worker_status=str(worker.get("model_status") or "none")
    if worker_model==name:
        detail=str(worker.get("model_error") or worker.get("detail") or "")
        return {**base,"status":worker_status,"detail":detail}
    if worker_status=="error" and not worker_model:
        return {**base,"status":"error","detail":str(worker.get("model_error") or worker.get("detail") or "Не удалось загрузить активную модель")}
    return {**base,"status":"waiting","detail":"Inference worker ещё не применил выбранную модель"}


@app.get("/api/models")
def models():
    active=rows("SELECT value FROM settings WHERE key='active_model'")[0]["value"]
    slots=active_model_slots()
    test_mode=con_value("model_test_mode","false")=="true"
    limits={r["key"]:float(r["value"]) for r in rows("SELECT key,value FROM settings WHERE key IN ('min_model_precision','min_model_recall')")}
    worker=inference_worker_state()
    data=rows("SELECT name,format,status,precision,recall,trained_at,source,artifact_uri,checksum FROM model_registry ORDER BY id DESC")
    for item in data:
        item["active"]=item["name"]==active
        item["slot_roles"]=[role for role,name in slots.items() if name==item["name"]]
        item["pipeline_active"]=bool(item["slot_roles"])
        item["trial_eligible"]=_is_trial_preset_source(item.get("source"))
        item["trial_mode"]=bool(item["active"] and item["trial_eligible"] and not _model_meets_quality(item.get("precision"),item.get("recall"),limits))
        item["test_mode"]=bool(item["active"] and test_mode)
        item["runtime"]=_model_runtime_view(str(item["name"]),bool(item["active"]),worker)
    return data


def _pipeline_settings_from_connection(con: sqlite3.Connection) -> dict[str,str]:
    row=con.execute("SELECT value FROM settings WHERE key='active_model_slots'").fetchone()
    try:
        raw=json.loads(row[0] if row else "{}")
    except (TypeError,ValueError,json.JSONDecodeError):
        raw={}
    return {role:name for role,name in (raw.items() if isinstance(raw,dict) else ()) if role in MODEL_PIPELINE_ROLES and isinstance(name,str) and re.fullmatch(r"[A-Za-z0-9._-]{2,120}",name)}


@app.get("/api/models/pipeline")
def model_pipeline():
    slots=active_model_slots()
    records={item["name"]:item for item in rows("SELECT name,format,status,precision,recall,source FROM model_registry WHERE status='ready'")}
    return {"roles":[{"id":role,"label":label,"model":slots.get(role),"ready":bool(slots.get(role) in records),"model_info":records.get(slots.get(role,""))} for role,label in MODEL_PIPELINE_ROLES.items()],"slots":slots}


@app.post("/api/models/{name}/activate-slot")
def activate_model_slot(name: str, payload: ModelSlotIn):
    con=db()
    try:
        model=con.execute("SELECT status,precision,recall,source,artifact_uri FROM model_registry WHERE name=?",(name,)).fetchone()
        if not model:
            raise HTTPException(404,"Модель не найдена")
        if model[0]!="ready":
            raise HTTPException(409,"Модель ещё не готова")
        _ensure_managed_model_artifact(model[3],model[4])
        limits={row[0]:float(row[1]) for row in con.execute("SELECT key,value FROM settings WHERE key IN ('min_model_precision','min_model_recall')").fetchall()}
        if not _model_meets_quality(model[1],model[2],limits):
            raise HTTPException(409,"Для production-контура отдельная модель должна пройти validation: укажите Precision и Recall не ниже quality gate или используйте тест на камере.")
        slots=_pipeline_settings_from_connection(con)
        slots[payload.role]=name
        con.execute("INSERT INTO settings(key,value) VALUES('active_model_slots',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(slots,ensure_ascii=False,sort_keys=True),))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","model_manager",f"Pipeline slot {payload.role} -> {name}"))
        con.commit()
        return {"role":payload.role,"model":name,"slots":slots}
    finally:
        con.close()


@app.delete("/api/models/pipeline/{role}")
def deactivate_model_slot(role: str):
    if role not in MODEL_PIPELINE_ROLES:
        raise HTTPException(404,"Слот модели не найден")
    con=db()
    try:
        slots=_pipeline_settings_from_connection(con)
        previous=slots.pop(role,None)
        con.execute("INSERT INTO settings(key,value) VALUES('active_model_slots',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(slots,ensure_ascii=False,sort_keys=True),))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","model_manager",f"Pipeline slot {role} cleared"))
        con.commit()
        return {"role":role,"cleared":bool(previous),"slots":slots}
    finally:
        con.close()


@app.post("/api/models",status_code=201)
def register_model(payload:ModelIn):
    con=db()
    try: con.execute("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)",(payload.name,payload.format,"ready",payload.precision,payload.recall,now_iso(),payload.source,payload.artifact_uri,payload.checksum)); con.commit()
    except sqlite3.IntegrityError: con.close(); raise HTTPException(409,"Модель с таким именем уже существует")
    con.close(); return {"name":payload.name,"status":"ready","registered":True}


MODEL_UPLOAD_EXTENSIONS={
    "ONNX":{".onnx"},
    "ONNX FP16":{".onnx"},
    "TensorRT":{".engine",".plan",".trt"},
    "TensorRT FP16":{".engine",".plan",".trt"},
    "PyTorch":{".pt",".pth"},
}


def _uploaded_model_file_info(model_format: str, filename: str) -> tuple[str,str]:
    """Return a safe original display name and compatible model extension."""
    raw=(filename or "").replace("\\","/").rsplit("/",1)[-1]
    extension=Path(raw).suffix.lower()
    allowed=MODEL_UPLOAD_EXTENSIONS.get(model_format,set())
    if not raw or extension not in allowed:
        expected=", ".join(sorted(allowed)) or "подходящий файл"
        raise HTTPException(422,f"Для формата {model_format} выберите файл: {expected}")
    # Never persist a browser-provided path/identifier as-is in the registry.
    safe_name=re.sub(r"[^A-Za-z0-9._-]+","_",raw).strip("._")[:180]
    return safe_name or f"model{extension}",extension


@app.post("/api/models/upload",status_code=201)
async def upload_model_file(
    request: Request,
    name: str=Query(min_length=2,max_length=120,pattern=r"^[a-zA-Z0-9._-]+$"),
    model_format: Literal["ONNX","ONNX FP16","TensorRT","TensorRT FP16","PyTorch"]=Query(alias="format"),
    precision: float|None=Query(default=None,ge=0,le=100),
    recall: float|None=Query(default=None,ge=0,le=100),
    filename: str=Query(min_length=1,max_length=255),
):
    """Stream a locally selected model into MODEL_DIR and register it atomically.

    The browser submits raw bytes rather than multipart form data, avoiding an
    extra dependency and allowing large files to be written chunk-by-chunk to
    the shared model volume.  The client-controlled filename only contributes
    a validated extension; the persisted filename is always derived from the
    validated model name.
    """
    if (precision is None) != (recall is None):
        raise HTTPException(422,"Укажите Precision и Recall вместе или оставьте оба поля пустыми для теста на камере")
    source_filename,extension=_uploaded_model_file_info(model_format,filename)
    if rows("SELECT 1 FROM model_registry WHERE name=?",(name,)):
        raise HTTPException(409,"Модель с таким именем уже существует")
    try:
        MODEL_DIR.mkdir(parents=True,exist_ok=True)
        target=MODEL_DIR/f"{name}{extension}"
        if target.exists():
            raise HTTPException(409,"Файл с таким именем уже есть в хранилище моделей; выберите другое имя модели")
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(503,"Хранилище моделей недоступно для записи") from exc

    temporary=MODEL_DIR/f".{name}-{uuid.uuid4().hex}{extension}.upload"
    digest=hashlib.sha256()
    total=0
    try:
        with temporary.open("xb") as stream:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total+=len(chunk)
                if total>MODEL_UPLOAD_MAX_BYTES:
                    limit_mb=max(1,(MODEL_UPLOAD_MAX_BYTES+999_999)//1_000_000)
                    raise HTTPException(413,f"Модель слишком большая (лимит {limit_mb} МБ)")
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if not total:
            raise HTTPException(422,"Файл модели пуст")
        # link() creates the final name only if it does not exist; unlike
        # replace(), this can never overwrite a concurrent upload or artifact.
        try:
            os.link(temporary,target)
        except FileExistsError:
            raise HTTPException(409,"Файл с таким именем уже есть в хранилище моделей; выберите другое имя модели")
        except OSError as exc:
            raise HTTPException(503,"Не удалось сохранить файл модели") from exc
    except HTTPException:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(503,"Не удалось записать файл модели") from exc
    finally:
        temporary.unlink(missing_ok=True)

    checksum=digest.hexdigest()
    source=f"upload:{source_filename}"
    con=db()
    try:
        con.execute("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)",(name,model_format,"ready",precision,recall,now_iso(),source,f"file://{target}",checksum))
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","model_manager",f"Model uploaded: {name}, format={model_format}, size={total}"))
        con.commit()
    except sqlite3.IntegrityError:
        target.unlink(missing_ok=True)
        raise HTTPException(409,"Модель с таким именем уже существует")
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(503,"Не удалось зарегистрировать загруженную модель") from exc
    finally:
        con.close()
    return {"name":name,"status":"ready","registered":True,"uploaded":True,"artifact_uri":f"file://{target}","checksum":checksum,"size_bytes":total,"source":source}


def _preset_artifact_extension(preset: dict[str,Any]) -> str:
    """Keep custom preset artifacts compatible with their declared format."""
    model_format=str(preset.get("format") or "")
    allowed=MODEL_UPLOAD_EXTENSIONS.get(model_format,set())
    if not allowed:
        raise HTTPException(422,f"Пресет {preset.get('id','')} имеет неподдерживаемый формат")
    extension=Path(urlparse(str(preset.get("url") or "")).path).suffix.lower()
    if extension in allowed:
        return extension
    # Built-in models are PyTorch; custom preset URLs may omit a suffix, so
    # choose the conventional extension for their declared runtime format.
    preferred={"ONNX":".onnx","ONNX FP16":".onnx","TensorRT":".engine","TensorRT FP16":".engine","PyTorch":".pt"}
    return preferred.get(model_format,min(allowed))


def _preset_view(preset:dict) -> dict:
    name=preset["name"]
    existing=rows("SELECT name,status,precision,recall,source FROM model_registry WHERE name=?",(name,))
    return {**{k:v for k,v in preset.items() if k!="url"},"downloaded":bool(existing),"registered":bool(existing),"source":existing[0]["source"] if existing else None}


@app.get("/api/models/presets")
def model_presets():
    return {"presets":[_preset_view(p) for p in MODEL_PRESETS]}


@app.post("/api/models/presets/{preset_id}/download",status_code=200)
def download_model_preset(preset_id:str):
    preset=next((p for p in MODEL_PRESETS if p["id"]==preset_id),None)
    if not preset: raise HTTPException(404,"Такой пресет не найден")
    name=preset["name"]
    existing=rows("SELECT name,source,status FROM model_registry WHERE name=?",(name,))
    if existing: return {"downloaded":False,"already":True,"model":name,"source":existing[0]["source"],"trial_activation":bool(preset.get("trial_activation")),"message":f"Модель {name} уже в реестре. Если файл отсутствует на диске, активация покажет ошибку."}
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,120}",name): raise HTTPException(422,"Недопустимое имя модели")
    try: minimum_bytes=max(1,int(preset.get("min_bytes",1)))
    except (TypeError,ValueError): minimum_bytes=1
    MODEL_DIR.mkdir(parents=True,exist_ok=True)
    ext=_preset_artifact_extension(preset)
    dest=MODEL_DIR/f"{name}{ext}"
    tmp=dest.with_suffix(ext+".tmp")
    try:
        with httpx.stream("GET",preset["url"],timeout=600,follow_redirects=True) as resp:
            resp.raise_for_status()
            total=0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk); total+=len(chunk)
                    if total>200_000_000: raise HTTPException(413,"Модель слишком большая")
        if total<minimum_bytes:
            raise HTTPException(502,"Скачанный файл модели слишком мал или повреждён")
        tmp.replace(dest)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except (httpx.HTTPError,OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(502,f"Не удалось скачать модель: {type(exc).__name__}") from exc
    digest=hashlib.sha256(dest.read_bytes()).hexdigest()
    con=db()
    try:
        con.execute("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)",(name,preset["format"],"ready",None,None,now_iso(),f"preset:{preset['id']}",f"file://{dest}",digest)); con.commit()
    except sqlite3.IntegrityError:
        con.close(); dest.unlink(missing_ok=True); raise HTTPException(409,"Модель с таким именем уже существует")
    con.close()
    trial=bool(preset.get("trial_activation"))
    message=("PPE-модель скачана. Нажмите «Включить PPE-тест», чтобы проверить её на своих камерах." if trial else f"Модель {name} скачана и зарегистрирована. Метрики не заданы — обучите на своих данных или укажите метрики валидации, затем активируйте.")
    return {"downloaded":True,"model":name,"artifact_uri":f"file://{dest}","size_bytes":total,"sha256":digest,"requires_validation":True,"trial_activation":trial,"message":message}


@app.get("/api/models/active/health")
def active_model_health():
    con=db()
    try:
        active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()
        model=con.execute("SELECT name,format,status,precision,recall,trained_at,source FROM model_registry WHERE name=?",(active[0],)).fetchone() if active else None
        last=con.execute("SELECT timestamp,message FROM logs WHERE service='inference_gateway' ORDER BY id DESC LIMIT 1").fetchone()
        limits={r[0]:float(r[1]) for r in con.execute("SELECT key,value FROM settings WHERE key IN ('min_model_precision','min_model_recall')").fetchall()}
    finally:
        con.close()
    if not model: raise HTTPException(503,"Активная модель отсутствует в реестре")
    healthy=model[2]=="ready" and _model_meets_quality(model[3],model[4],limits)
    trial_mode=bool(not healthy and _is_trial_preset_source(model[6]))
    test_mode=con_value("model_test_mode","false")=="true"
    runtime=_model_runtime_view(str(model[0]),True,inference_worker_state())
    return {"healthy":healthy,"trial_mode":trial_mode,"test_mode":test_mode,"model":dict(model),"runtime":runtime,"requirements":{"precision":limits.get('min_model_precision',90),"recall":limits.get('min_model_recall',85)},"last_inference":dict(last) if last else None}


def _ensure_managed_model_artifact(source: str | None, artifact_uri: str | None) -> None:
    """Fail early when an uploaded/preset artifact vanished from model-data."""
    if not source or not source.startswith(("upload:","preset:")):
        return
    if not str(artifact_uri or "").startswith("file://"):
        raise HTTPException(409,"Управляемый артефакт модели имеет некорректный путь")
    try:
        artifact=Path(str(artifact_uri).removeprefix("file://")).resolve()
        base=MODEL_DIR.resolve()
    except (OSError,ValueError) as exc:
        raise HTTPException(409,"Не удалось проверить файл модели в хранилище") from exc
    if base not in artifact.parents or not artifact.is_file():
        raise HTTPException(409,"Файл модели отсутствует в общем хранилище. Загрузите модель заново.")


def _activate_model(name:str, *, allow_trial:bool=False, allow_test:bool=False):
    started=time.perf_counter()
    con=db()
    try:
        model=con.execute("SELECT status,precision,recall,source,artifact_uri FROM model_registry WHERE name=?",(name,)).fetchone()
        if not model: raise HTTPException(404,"Модель не найдена")
        if model[0] != "ready": raise HTTPException(409,"Модель ещё не готова")
        _ensure_managed_model_artifact(model[3],model[4])
        limits={r[0]:float(r[1]) for r in con.execute("SELECT key,value FROM settings WHERE key IN ('min_model_precision','min_model_recall')").fetchall()}
        quality_ok=_model_meets_quality(model[1],model[2],limits)
        trial_mode=not quality_ok and allow_trial and _is_trial_preset_source(model[3])
        # A camera test is explicit and visibly marked in the UI. It lets an
        # operator inspect a newly uploaded model's real boxes before claiming
        # its supplied metrics are good enough for production alarms.
        test_mode=trial_mode or allow_test
        if not quality_ok and not test_mode:
            if model[1] is None or model[2] is None:
                raise HTTPException(409,"У модели отсутствуют метрики валидации. Нажмите «Тест на камере», чтобы проверить её без production-активации.")
            raise HTTPException(409,"Метрики модели ниже минимально допустимых. Для проверки на камере используйте тестовый запуск.")
        con.execute("BEGIN IMMEDIATE")
        old=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
        if old==name:
            con.execute("UPDATE settings SET value='false' WHERE key='active_model_disabled'")
            con.execute("UPDATE settings SET value=? WHERE key='model_test_mode'",("true" if test_mode else "false",))
            con.commit()
            return {"active_model":name,"previous_model":old,"hot_swap":False,"idempotent":True,"trial_mode":trial_mode,"test_mode":test_mode,"control_plane_switch_ms":round((time.perf_counter()-started)*1000,2),"downtime_ms":0}
        con.execute("UPDATE settings SET value=? WHERE key='active_model'",(name,))
        # Keep a validated model selected before a PPE test so stopping the
        # test restores the exact previous state instead of leaving an
        # operator unexpectedly without analytics.
        con.execute("UPDATE settings SET value=? WHERE key='ppe_trial_previous_model'",(old if trial_mode else "",))
        con.execute("UPDATE settings SET value=? WHERE key='model_test_mode'",("true" if test_mode else "false",))
        con.execute("UPDATE settings SET value='false' WHERE key='active_model_disabled'")
        level="WARNING" if test_mode else "INFO"
        mode="PPE trial" if trial_mode else "camera test" if test_mode else "validated"
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),level,"model_manager",f"Control-plane hot-swap {old} -> {name} ({mode}) completed"))
        con.commit()
        return {"active_model":name,"previous_model":old,"hot_swap":True,"idempotent":False,"trial_mode":trial_mode,"test_mode":test_mode,"control_plane_switch_ms":round((time.perf_counter()-started)*1000,2),"downtime_ms":0}
    finally:
        con.close()


@app.post("/api/models/{name}/activate")
def activate(name:str):
    return _activate_model(name)


@app.post("/api/models/{name}/activate-trial")
def activate_trial_model(name:str):
    """Explicitly enable only the selected PPE baseline for an on-site trial."""
    return _activate_model(name,allow_trial=True)


@app.post("/api/models/{name}/activate-test")
def activate_test_model(name:str):
    """Run any ready model on cameras as an explicitly non-production test."""
    return _activate_model(name,allow_test=True)


@app.post("/api/models/{name}/deactivate-trial")
def deactivate_trial_model(name:str):
    """Stop the explicit PPE trial and leave camera preview running."""
    con=db()
    try:
        row=con.execute("SELECT source FROM model_registry WHERE name=?",(name,)).fetchone()
        if not row: raise HTTPException(404,"Модель не найдена")
        if not _is_trial_preset_source(row[0]): raise HTTPException(409,"Остановить через этот маршрут можно только PPE-тест")
        active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
        if active != name:
            return {"active_model":active,"stopped":False,"idempotent":True}
        previous_row=con.execute("SELECT value FROM settings WHERE key='ppe_trial_previous_model'").fetchone()
        previous=previous_row[0] if previous_row else ""
        restored=""
        if previous and con.execute("SELECT 1 FROM model_registry WHERE name=? AND status='ready'",(previous,)).fetchone():
            restored=previous
        con.execute("UPDATE settings SET value=? WHERE key='active_model'",(restored,))
        con.execute("UPDATE settings SET value=? WHERE key='active_model_disabled'",("false" if restored else "true",))
        con.execute("UPDATE settings SET value='false' WHERE key='model_test_mode'")
        con.execute("UPDATE settings SET value='' WHERE key='ppe_trial_previous_model'")
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","model_manager",f"PPE trial stopped: {name}; restored={restored or 'none'}"))
        con.commit()
        return {"active_model":restored,"restored_model":restored or None,"stopped":True,"idempotent":False}
    finally:
        con.close()


@app.post("/api/models/{name}/deactivate-test")
def deactivate_test_model(name:str):
    """Stop an explicit camera test while retaining the model in the registry."""
    con=db()
    try:
        row=con.execute("SELECT source FROM model_registry WHERE name=?",(name,)).fetchone()
        if not row: raise HTTPException(404,"Модель не найдена")
        active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
        testing=con.execute("SELECT value FROM settings WHERE key='model_test_mode'").fetchone()
        if active != name or not testing or testing[0]!="true":
            return {"active_model":active or None,"stopped":False,"idempotent":True}
        if _is_trial_preset_source(row[0]):
            # Preserve the PPE-specific restore behaviour for its previous
            # validated model rather than unexpectedly leaving it disabled.
            con.close()
            con = None
            return deactivate_trial_model(name)
        con.execute("UPDATE settings SET value='' WHERE key='active_model'")
        con.execute("UPDATE settings SET value='true' WHERE key='active_model_disabled'")
        con.execute("UPDATE settings SET value='false' WHERE key='model_test_mode'")
        con.execute("UPDATE settings SET value='' WHERE key='ppe_trial_previous_model'")
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","model_manager",f"Camera test stopped: {name}"))
        con.commit()
        return {"active_model":None,"stopped":True,"idempotent":False}
    finally:
        if con is not None:
            con.close()

def _delete_model(name:str, *, deactivate:bool=False):
    """Remove a model and optionally stop it first when it is currently active."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,120}",name or ""): raise HTTPException(422,"Недопустимое имя модели")
    con=db(); row=con.execute("SELECT status,source,artifact_uri FROM model_registry WHERE name=?",(name,)).fetchone()
    if not row: con.close(); raise HTTPException(404,"Модель не найдена")
    source=row[1]; artifact_uri=row[2] or ""
    jobs=con.execute("SELECT COUNT(*) FROM training_jobs WHERE target_name=? AND status IN ('queued','running')",(name,)).fetchone()[0]
    if jobs: con.close(); raise HTTPException(409,"Модель используется текущей задачей обучения")
    active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
    active_after=active
    deactivated_active=False
    if active==name:
        if not deactivate:
            con.close(); raise HTTPException(409,"Модель активна. Сначала переключитесь на другую или подтвердите остановку и удаление.")
        deactivated_active=True
        # Deleting a currently running PPE trial restores its validated model
        # when possible; regular models leave inference deliberately disabled.
        previous_row=con.execute("SELECT value FROM settings WHERE key='ppe_trial_previous_model'").fetchone()
        previous=previous_row[0] if previous_row else ""
        restored=""
        if _is_trial_preset_source(source) and previous and previous!=name and con.execute("SELECT 1 FROM model_registry WHERE name=? AND status='ready'",(previous,)).fetchone():
            restored=previous
        active_after=restored
        con.execute("UPDATE settings SET value=? WHERE key='active_model'",(active_after,))
        con.execute("UPDATE settings SET value=? WHERE key='active_model_disabled'",("false" if active_after else "true",))
        con.execute("UPDATE settings SET value='false' WHERE key='model_test_mode'")
        con.execute("UPDATE settings SET value='' WHERE key='ppe_trial_previous_model'")
    slots=_pipeline_settings_from_connection(con)
    removed_slots=[role for role,model_name in slots.items() if model_name==name]
    if removed_slots:
        slots={role:model_name for role,model_name in slots.items() if model_name!=name}
        con.execute("INSERT INTO settings(key,value) VALUES('active_model_slots',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(slots,ensure_ascii=False,sort_keys=True),))
    con.execute("DELETE FROM model_registry WHERE name=?",(name,))
    con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","model_manager",f"Model deleted: {name} (source={source}, deactivated={deactivated_active}, slots={','.join(removed_slots) or 'none'})"))
    con.commit(); con.close()
    removed_file=False
    if source and source.startswith(("preset:","upload:")) and artifact_uri.startswith("file://"):
        try:
            artifact=Path(artifact_uri.removeprefix("file://")).resolve()
            base=MODEL_DIR.resolve()
            if base.exists() and base in artifact.parents and artifact.is_file():
                artifact.unlink(); removed_file=True
        except (OSError,ValueError):
            removed_file=False
    return {"name":name,"deleted":True,"removed_artifact_file":removed_file,"source":source,"deactivated_active":deactivated_active,"active_model":active_after or None}


@app.delete("/api/models/{name}")
def delete_model(name:str, deactivate:bool=False):
    """Delete one model; an active model needs explicit deactivation consent."""
    return _delete_model(name,deactivate=deactivate)


@app.post("/api/models/delete-bulk")
def delete_models_bulk(payload:ModelBulkDeleteIn):
    """Delete a selected group while reporting every blocked item explicitly.

    Each model is handled independently so a training job or missing record
    does not hide successful deletions from the operator.
    """
    deleted=[]
    failed=[]
    for name in payload.names:
        try:
            deleted.append(_delete_model(name,deactivate=payload.deactivate_active))
        except HTTPException as exc:
            failed.append({"name":name,"status":exc.status_code,"detail":str(exc.detail)})
    return {"deleted":deleted,"failed":failed,"requested":len(payload.names)}


IMAGE_EXTS={".jpg",".jpeg",".png",".bmp",".webp",".tif",".tiff"}
VIDEO_EXTS={".mp4",".avi",".mov",".mkv",".m4v",".webm",".mpg",".mpeg",".wmv"}
def _slugify(name:str) -> str:
    keep="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
    return "".join(ch if ch in keep else "_" for ch in name).strip("._") or "dataset"
def _find_dataset_yaml(root:Path) -> Path|None:
    if (root/"data.yaml").is_file(): return root/"data.yaml"
    if (root/"dataset.yaml").is_file(): return root/"dataset.yaml"
    for sub in root.iterdir():
        if sub.is_dir():
            if (sub/"data.yaml").is_file(): return sub/"data.yaml"
            if (sub/"dataset.yaml").is_file(): return sub/"dataset.yaml"
    return None
def _count_ext(root:Path,exts:set[str]) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts)

def _find_media_root(root:Path) -> Path:
    """If the zip unpacked to a single wrapper dir, descend into it."""
    if any(p.is_dir() for p in root.iterdir()) and not any(p.is_file() for p in root.iterdir()):
        subs=[p for p in root.iterdir() if p.is_dir()]
        if len(subs)==1: return subs[0]
    return root

def _inspect_dataset(root:Path) -> dict:
    """Auto-detect and validate an uploaded dataset bundle and its kind.

    Kinds:
      yolo   - data.yaml + images/ + labels/ (fully labelled dataset)
      images - a plain pack of photos (auto-labelled by the training worker)
      videos - a pack of video files (frames auto-extracted + auto-labelled)
    Returns a dict with the staging root and media stats; raises ValueError
    if the bundle is not usable.
    """
    base=_find_media_root(root)
    data_yaml=_find_dataset_yaml(base)
    images=_count_ext(base,IMAGE_EXTS)
    videos=_count_ext(base,VIDEO_EXTS)
    labels=_count_ext(base,{".txt"})
    if data_yaml is not None and (base/"images").exists():
        try: cfg=yaml.safe_load(data_yaml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc: raise ValueError(f"Некорректный data.yaml: {exc}") from exc
        names=cfg.get("names")
        if not names: raise ValueError("В data.yaml отсутствует список классов 'names'")
        class_count=len(names) if isinstance(names,(list,dict)) else 0
        if images<10: raise ValueError(f"В датасете только {images} изображений (нужно минимум 10)")
        return {"root":base,"data_yaml":data_yaml,"names":names,"class_count":class_count,"image_count":images,"label_count":labels,"val_split":bool(cfg.get("val")),"kind":"yolo"}
    if videos>0:
        return {"root":base,"data_yaml":None,"names":None,"class_count":0,"image_count":0,"label_count":0,"val_split":False,"kind":"videos","media_count":videos}
    if images>=10:
        return {"root":base,"data_yaml":None,"names":None,"class_count":0,"image_count":images,"label_count":0,"val_split":False,"kind":"images","media_count":images}
    if images<10 and images>0: raise ValueError(f"Слишком мало изображений ({images}), нужно минимум 10")
    raise ValueError("Архив должен содержать картинки (.jpg/.png/...), видео (.mp4/...) или YOLO-датасет с data.yaml")
@app.get("/api/datasets")
def list_datasets():
    data=rows("SELECT id,name,image_count,class_count,media_count,label_count,kind,created_at FROM datasets ORDER BY id DESC")
    for item in data:
        item["path"]=str(DATASET_DIR/item["name"]); item["exists"]=Path(item["path"]).is_dir()
    return data

def _safe_extract_zip(zf,dest):
    """Extract members individually, enforcing the resolutions already checked
    (no absolute/../ paths, no links/devices) and a total decompressed cap."""
    total=0
    for member in zf.infolist():
        if member.is_dir(): dest.mkdir(parents=True,exist_ok=True); continue
        target=dest.joinpath(*[p for p in Path(member.filename).parts if p not in ("",".")])
        target.parent.mkdir(parents=True,exist_ok=True)
        with zf.open(member) as src, open(target,"wb") as out:
            while True:
                chunk=src.read(1024*1024)
                if not chunk: break
                total+=len(chunk)
                if total>2_000_000_000: raise HTTPException(413,"Архив распаковывается слишком большим (лимит 2 ГБ)")
                out.write(chunk)

@app.post("/api/datasets",status_code=201)
def upload_dataset(name:str=Query(min_length=2,max_length=120),payload:bytes=Body(...)):
    safe=_slugify(name)
    if rows("SELECT 1 FROM datasets WHERE name=?",(safe,)):
        raise HTTPException(409,"Датасет с таким именем уже существует")
    if len(payload)<100 or payload[:4]!=b"PK\x03\x04":
        raise HTTPException(422,"Ожидался zip-архив датасета")
    workdir=Path(tempfile.mkdtemp(prefix="zmk-ds-"))
    try:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                if len(zf.infolist())>15000: raise HTTPException(413,"Слишком много файлов в архиве (лимит 15000)")
                total_uncompressed=0
                for member in zf.infolist():
                    if member.is_dir(): continue
                    member_path=Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts or member_path.suffix.lower() in {".sh",".exe",".bat",".cmd",".ps1"}:
                        raise HTTPException(422,f"Недопустимый файл в архиве: {member.filename}")
                    # Reject dangerous link/device entries (zip-bomb / symlink tampering).
                    if (member.external_attr >> 16) & 0o170000 in (0o120000,0o060000,0o020000):
                        raise HTTPException(422,f"Ссылки/устройства недопустимы: {member.filename}")
                    if member.file_size>1_000_000_000: raise HTTPException(413,f"Файл слишком велик: {member.filename}")
                    total_uncompressed+=member.file_size
                    if total_uncompressed>2_000_000_000: raise HTTPException(413,"Архив распаковывается слишком большим (лимит 2 ГБ)")
                _safe_extract_zip(zf,workdir)
        except zipfile.BadZipFile as exc: raise HTTPException(422,"Повреждённый zip-архив") from exc
        try: inspected=_inspect_dataset(workdir)
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
        target=DATASET_DIR/safe; DATASET_DIR.mkdir(parents=True,exist_ok=True)
        if target.exists(): shutil.rmtree(target)
        shutil.copytree(inspected["root"],target)
        media=inspected.get("media_count",inspected["image_count"])
        con=db(); cur=con.execute("INSERT INTO datasets(name,path,image_count,class_count,created_at,kind,media_count,label_count) VALUES(?,?,?,?,?,?,?,?)",(safe,str(target),inspected["image_count"],inspected["class_count"],now_iso(),inspected["kind"],media,inspected["label_count"])); con.commit(); ds_id=cur.lastrowid; con.close()
        return {"id":ds_id,"name":safe,"image_count":inspected["image_count"],"class_count":inspected["class_count"],"label_count":inspected["label_count"],"media_count":media,"kind":inspected["kind"],"val_split":inspected["val_split"]}
    finally:
        shutil.rmtree(workdir,ignore_errors=True)
@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id:int):
    """Delete a saved dataset and its files without hiding a failed removal.

    A failed filesystem operation used to be ignored, so the UI could report a
    successful deletion while the data still occupied the dataset volume.  Do
    the filesystem step first and leave the registry entry intact on an error,
    which gives the operator a retryable, honest result.
    """
    con=db()
    try:
        row=con.execute("SELECT id,name FROM datasets WHERE id=?",(dataset_id,)).fetchone()
        if not row:
            raise HTTPException(404,"Датасет не найден")
        name=row["name"]
        training=con.execute("SELECT id,target_name FROM training_jobs WHERE dataset_name=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",(name,)).fetchone()
        if training:
            raise HTTPException(409,f"Датасет используется задачей обучения «{training['target_name']}» (№{training['id']}). Сначала отмените её или дождитесь завершения.")
        capture=con.execute("SELECT id FROM dataset_capture_jobs WHERE name=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",(name,)).fetchone()
        if capture:
            raise HTTPException(409,f"Датасет сейчас собирается (задача №{capture['id']}). Сначала отмените сбор.")

        # Dataset names are slugified on input, and the API always constructs
        # the path from that name rather than trusting the database path.
        target=DATASET_DIR/name
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                # A stale file/symlink must not survive after its registry row
                # is removed; unlinking a symlink never follows it.
                target.unlink()
        except OSError as exc:
            raise HTTPException(500,f"Не удалось удалить файлы датасета: {exc.strerror or str(exc)}") from exc

        con.execute("DELETE FROM datasets WHERE id=?",(dataset_id,))
        # A deleted camera-captured dataset should be collectable again under
        # the same name. Keep active jobs protected above, but clear terminal
        # history records whose unique name would otherwise block it forever.
        con.execute("DELETE FROM dataset_capture_jobs WHERE name=? AND status NOT IN ('queued','running')",(name,))
        con.commit()
        return {"id":dataset_id,"deleted":True,"name":name}
    finally:
        con.close()

class DatasetCaptureIn(BaseModel):
    camera_id:str=Field(min_length=1,max_length=64)
    name:str=Field(min_length=2,max_length=120)
    image_count:int=Field(default=100,ge=20,le=5000)
    capture_fps:float=Field(default=2,ge=.1,le=20)

async def collect_camera_dataset(job_id:int) -> None:
    target:Path|None=None
    try:
        con=db(); job=con.execute("SELECT name,camera_id,target_count,capture_fps FROM dataset_capture_jobs WHERE id=?",(job_id,)).fetchone()
        if not job: con.close(); return
        con.execute("UPDATE dataset_capture_jobs SET status='running',stage='Ожидание live-кадров',updated_at=? WHERE id=?",(now_iso(),job_id)); con.commit(); con.close()
        name,camera_id,target_count,capture_fps=job[0],job[1],job[2],job[3]
        target=DATASET_DIR/name; images=target/'images'; images.mkdir(parents=True,exist_ok=False)
        last_sequence=-1; captured=0; last_progress=0.; no_frame_since=time.monotonic()
        interval=1/max(.1,float(capture_fps))
        while captured<target_count:
            with _live_frames_lock: frame=_live_frames.get(camera_id)
            if frame and frame[0]!=last_sequence:
                started=time.monotonic(); sequence,_,jpeg=frame
                path=images/f'frame_{captured+1:06}.jpg'; temp=path.with_suffix('.tmp'); temp.write_bytes(jpeg); temp.replace(path)
                last_sequence=sequence; captured+=1; no_frame_since=time.monotonic()
                if captured==target_count or time.monotonic()-last_progress>=.5:
                    con=db(); con.execute("UPDATE dataset_capture_jobs SET captured_count=?,stage='Сбор кадров',updated_at=? WHERE id=?",(captured,now_iso(),job_id)); con.commit(); con.close(); last_progress=time.monotonic()
                await asyncio.sleep(max(0.,interval-(time.monotonic()-started)))
                continue
            if time.monotonic()-no_frame_since>45: raise RuntimeError('Нет live-кадров от камеры более 45 секунд')
            await asyncio.sleep(.05)
        con=db(); cur=con.execute("INSERT INTO datasets(name,path,image_count,class_count,created_at,kind,media_count,label_count) VALUES(?,?,?,?,?,?,?,?)",(name,str(target),captured,0,now_iso(),'images',captured,0)); dataset_id=cur.lastrowid
        con.execute("UPDATE dataset_capture_jobs SET status='completed',captured_count=?,stage='Датасет готов',updated_at=? WHERE id=?",(captured,now_iso(),job_id)); con.commit(); con.close()
        _=dataset_id
    except asyncio.CancelledError:
        con=db(); con.execute("UPDATE dataset_capture_jobs SET status='cancelled',stage='Отменено',updated_at=? WHERE id=?",(now_iso(),job_id)); con.commit(); con.close()
        if target: shutil.rmtree(target,ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - show real capture error to operator.
        con=db(); con.execute("UPDATE dataset_capture_jobs SET status='failed',stage='Ошибка',error=?,updated_at=? WHERE id=?",(str(exc)[:500],now_iso(),job_id)); con.commit(); con.close()
        if target: shutil.rmtree(target,ignore_errors=True)
    finally:
        _dataset_capture_tasks.pop(job_id,None)

@app.post("/api/datasets/capture",status_code=202)
async def capture_dataset_from_camera(payload:DatasetCaptureIn):
    name=_slugify(payload.name)
    con=db(); camera=con.execute("SELECT enabled FROM cameras WHERE id=?",(payload.camera_id,)).fetchone()
    if not camera: con.close(); raise HTTPException(404,'Камера не найдена')
    if not camera[0]: con.close(); raise HTTPException(409,'Аналитика камеры отключена')
    if con.execute("SELECT 1 FROM datasets WHERE name=?",(name,)).fetchone(): con.close(); raise HTTPException(409,'Датасет с таким именем уже существует')
    previous_job=con.execute("SELECT id,status FROM dataset_capture_jobs WHERE name=?",(name,)).fetchone()
    if previous_job:
        previous_task=_dataset_capture_tasks.get(previous_job["id"])
        if previous_job["status"] in {'queued','running'}:
            con.close(); raise HTTPException(409,f'Сбор с таким именем уже выполняется (задача №{previous_job["id"]})')
        # cancel_dataset_capture marks the database record first and the task
        # removes its temporary directory moments later. Do not start another
        # job with the same directory until that cleanup has actually ended.
        if previous_task and not previous_task.done():
            con.close(); raise HTTPException(409,'Предыдущий сбор ещё отменяется; повторите через несколько секунд')
        # Terminal jobs are history, not a permanent reservation of a dataset
        # name. This also lets an operator repeat a failed/cancelled collection.
        con.execute("DELETE FROM dataset_capture_jobs WHERE name=?",(name,))
    cur=con.execute("INSERT INTO dataset_capture_jobs(name,camera_id,target_count,capture_fps,status,captured_count,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(name,payload.camera_id,payload.image_count,payload.capture_fps,'queued',0,'В очереди',now_iso(),now_iso())); con.commit(); job_id=cur.lastrowid; con.close()
    task=asyncio.create_task(collect_camera_dataset(job_id),name=f'dataset-capture-{job_id}'); _dataset_capture_tasks[job_id]=task
    return {'id':job_id,'name':name,'camera_id':payload.camera_id,'target_count':payload.image_count,'capture_fps':payload.capture_fps,'status':'queued'}

@app.get("/api/datasets/capture/jobs")
def dataset_capture_jobs(): return rows("SELECT * FROM dataset_capture_jobs ORDER BY id DESC LIMIT 20")

@app.post("/api/datasets/capture/jobs/{job_id}/cancel")
def cancel_dataset_capture(job_id:int):
    con=db(); row=con.execute("SELECT status FROM dataset_capture_jobs WHERE id=?",(job_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404,'Задача сбора не найдена')
    if row[0] not in {'queued','running'}: con.close(); raise HTTPException(409,'Задача уже завершена')
    con.execute("UPDATE dataset_capture_jobs SET status='cancelled',stage='Отменено оператором',updated_at=? WHERE id=?",(now_iso(),job_id)); con.commit(); con.close()
    task=_dataset_capture_tasks.get(job_id)
    if task: task.cancel()
    return {'id':job_id,'status':'cancelled'}

class DatasetPreviewIn(BaseModel):
    confidence:float|None=Field(default=None,ge=.05,le=.95)
    limit:int=Field(default=5,ge=1,le=12)

@app.post("/api/datasets/{dataset_name}/preview")
def preview_dataset(dataset_name:str,payload:DatasetPreviewIn|None=None):
    row=rows("SELECT name,path,kind FROM datasets WHERE name=?",(dataset_name,))
    if not row: raise HTTPException(404,"Датасет не найден")
    ds=row[0]
    if not Path(ds["path"]).is_dir(): raise HTTPException(503,"Файлы датасета не найдены на диске")
    if not TRAINING_WORKER_URL:
        raise HTTPException(503,"Сервис обучения не подключён. Проверьте контейнер training-worker")
    active=con_value("active_model","")
    artifact=None
    if active:
        m=rows("SELECT artifact_uri FROM model_registry WHERE name=? AND status='ready'",(active,))
        artifact=m[0]["artifact_uri"] if m else None
    conf=payload.confidence if payload and payload.confidence is not None else None
    limit=payload.limit if payload else 5
    body={"dataset_path":ds["path"],"base_artifact":artifact,"confidence":conf,"limit":limit,"kind":ds.get("kind") or "yolo"}
    try:
        response=httpx.post(f"{TRAINING_WORKER_URL}/preview",json=body,timeout=120)
        if response.status_code==400:
            raise HTTPException(422,response.json().get("detail","preview failed"))
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(503,f"Worker недоступен: {type(exc).__name__}") from exc

def con_value(key:str,default:str) -> str:
    row=rows("SELECT value FROM settings WHERE key=?",(key,))
    return row[0]["value"] if row else default

async def run_training(job_id:int):
    con=db(); con.execute("UPDATE training_jobs SET status='failed',stage='Worker не подключён',error='External GPU training worker is not configured',updated_at=? WHERE id=?",(now_iso(),job_id)); con.commit(); con.close()

@app.post("/api/training/jobs",status_code=202)
async def start_training(payload:TrainingIn):
    if not SEED_TEST_DATA and not TRAINING_WORKER_URL: raise HTTPException(503,"Сервис обучения не подключён. Проверьте контейнер training-worker")
    con=db(); cam=None; rtsp=""
    dataset_kind="yolo"
    if payload.source=="dataset":
        ds=con.execute("SELECT name,path,kind FROM datasets WHERE name=?",(payload.dataset_name or "",)).fetchone()
        if not ds: con.close(); raise HTTPException(404,"Датасет не найден. Загрузите его на вкладке Модели")
        if not Path(ds[1]).is_dir(): con.close(); raise HTTPException(503,"Файлы датасета не найдены на диске")
        dataset_kind=ds[2] or "yolo"
    else:
        cam=con.execute("SELECT status,rtsp_url,fps_limit FROM cameras WHERE id=?",(payload.camera_id,)).fetchone()
        if not cam: con.close(); raise HTTPException(404,"Камера не найдена")
        if cam[0] != "online": con.close(); raise HTTPException(409,"Камера офлайн: кадры недоступны")
        rtsp=cam[1]
    active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
    test_mode_row=con.execute("SELECT value FROM settings WHERE key='model_test_mode'").fetchone()
    if test_mode_row and test_mode_row[0]=='true':
        # A visual camera test is intentionally not promoted into the training
        # baseline; fine-tuning falls back to the validated/default YOLO base.
        active=""
    suffix=payload.dataset_name or payload.camera_id
    target=payload.target_name or f"siz-auto-{suffix}-{datetime.now(TZ).strftime('%m%d-%H%M%S')}"
    if con.execute("SELECT 1 FROM model_registry WHERE name=?",(target,)).fetchone() or con.execute("SELECT 1 FROM training_jobs WHERE target_name=? AND status IN ('queued','running')",(target,)).fetchone():
        con.close(); raise HTTPException(409,"Имя модели уже используется")
    if con.execute("SELECT 1 FROM training_jobs WHERE status IN ('queued','running')").fetchone():
        con.close(); raise HTTPException(409,"Уже выполняется другая задача обучения")
    dataset_name=payload.dataset_name or ""
    # dataset-mode jobs may have no camera, so camera_id is allowed to be empty
    # for them; relax the FK for this single insert only.
    if payload.source=="dataset":
        con.execute("PRAGMA foreign_keys=OFF")
    cur=con.execute("INSERT INTO training_jobs(created_at,updated_at,camera_id,base_model,target_name,image_count,epochs,status,progress,stage,batch,imgsz,patience,confidence,val_split,capture_fps,source,dataset_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(now_iso(),now_iso(),payload.camera_id,active,target,payload.image_count,payload.epochs,"queued",0,"В очереди",payload.batch,payload.imgsz,payload.patience,payload.confidence,payload.val_split,payload.capture_fps,payload.source,dataset_name)); con.commit(); jid=cur.lastrowid; con.close()
    if SEED_TEST_DATA:
        task=asyncio.create_task(run_training(jid),name=f"training-{jid}"); _training_tasks[jid]=task; task.add_done_callback(lambda _: _training_tasks.pop(jid,None))
    else:
        model=rows("SELECT artifact_uri FROM model_registry WHERE name=?",(active,))
        if payload.source=="dataset":
            dataset_path=(DATASET_DIR/dataset_name)
            request={"id":jid,"source":"dataset","dataset_path":str(dataset_path),"dataset_kind":dataset_kind,"target_name":target,"base_artifact":model[0]["artifact_uri"] if model else None,"image_count":payload.image_count,"epochs":payload.epochs,"batch":payload.batch,"imgsz":payload.imgsz,"patience":payload.patience,"confidence":payload.confidence,"val_split":payload.val_split}
        else:
            request={"id":jid,"source":"camera","camera_id":payload.camera_id,"rtsp_url":rtsp,"target_name":target,"base_artifact":model[0]["artifact_uri"] if model else None,"image_count":payload.image_count,"epochs":payload.epochs,"fps_limit":min(float(cam[2]),payload.capture_fps),"batch":payload.batch,"imgsz":payload.imgsz,"patience":payload.patience,"confidence":payload.confidence,"val_split":payload.val_split}
        try:
            async with httpx.AsyncClient(timeout=15) as client: response=await client.post(f"{TRAINING_WORKER_URL}/jobs",json=request); response.raise_for_status()
        except httpx.HTTPError as exc:
            con=db(); con.execute("UPDATE training_jobs SET status='failed',stage='Worker недоступен',error=?,updated_at=? WHERE id=?",(str(exc)[:500],now_iso(),jid)); con.commit(); con.close(); raise HTTPException(503,"Training worker недоступен")
    return {"id":jid,"status":"queued","target_name":target,"mode":"dataset" if payload.source=="dataset" else "pseudo-label fine-tuning","source":payload.source,"dataset_kind":dataset_kind if payload.source=="dataset" else None}

@app.put("/api/training/jobs/{job_id}/progress")
def training_progress(job_id:int,payload:TrainingProgress):
    con=db(); job=con.execute("SELECT target_name,camera_id,source,dataset_name FROM training_jobs WHERE id=?",(job_id,)).fetchone()
    if not job: con.close(); raise HTTPException(404,"Задача не найдена")
    con.execute("UPDATE training_jobs SET status=?,progress=?,stage=?,error=?,updated_at=? WHERE id=?",(payload.status,payload.progress,payload.stage,payload.error,now_iso(),job_id))
    if payload.status=="completed" and payload.artifact_uri and payload.precision is not None and payload.recall is not None:
        origin=f"dataset:{job[3]}" if job[2]=="dataset" else f"camera:{job[1]}"
        con.execute("INSERT OR REPLACE INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)",(job[0],"ONNX","ready",payload.precision,payload.recall,now_iso(),origin,payload.artifact_uri,""))
    con.commit(); con.close(); return {"id":job_id,"status":payload.status,"progress":payload.progress}

@app.get("/api/training/jobs")
def training_jobs(): return rows("SELECT * FROM training_jobs ORDER BY id DESC LIMIT 30")
@app.get("/api/training/jobs/{job_id}")
def training_job(job_id:int):
    data=rows("SELECT * FROM training_jobs WHERE id=?",(job_id,))
    if not data: raise HTTPException(404,"Задача не найдена")
    return data[0]
@app.post("/api/training/jobs/{job_id}/cancel")
def cancel_training(job_id:int):
    con=db(); job=con.execute("SELECT status FROM training_jobs WHERE id=?",(job_id,)).fetchone()
    if not job: con.close(); raise HTTPException(404,"Задача не найдена")
    if job[0] not in {"queued","running"}: con.close(); raise HTTPException(409,"Задача уже завершена")
    con.execute("UPDATE training_jobs SET status='cancelled',stage='Отменено оператором',updated_at=? WHERE id=?",(now_iso(),job_id)); con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","training",f"Job {job_id} cancelled by operator")); con.commit(); con.close()
    task=_training_tasks.get(job_id)
    if task: task.cancel()
    elif TRAINING_WORKER_URL:
        try: httpx.delete(f"{TRAINING_WORKER_URL}/jobs/{job_id}",timeout=5)
        except httpx.HTTPError: pass
    return {"id":job_id,"status":"cancelled"}

@app.get("/api/admin/summary")
def admin_summary():
    con=db(); result={"users":con.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0],"audit24h":con.execute("SELECT COUNT(*) FROM logs WHERE timestamp>=?",((datetime.now(TZ)-timedelta(days=1)).isoformat(),)).fetchone()[0],"errors24h":con.execute("SELECT COUNT(*) FROM logs WHERE level IN ('ERROR','CRITICAL') AND timestamp>=?",((datetime.now(TZ)-timedelta(days=1)).isoformat(),)).fetchone()[0],"training_running":con.execute("SELECT COUNT(*) FROM training_jobs WHERE status IN ('queued','running')").fetchone()[0]}; con.close(); return result
@app.get("/api/reports/errors")
def error_report(hours:int=Query(24,ge=1,le=720)):
    since=(datetime.now(TZ)-timedelta(hours=hours)).isoformat()
    items=rows("SELECT * FROM logs WHERE level IN ('WARNING','ERROR','CRITICAL') AND timestamp>=? ORDER BY id DESC",(since,))
    summary={level:sum(1 for x in items if x['level']==level) for level in ['WARNING','ERROR','CRITICAL']}
    return {"period_hours":hours,"generated_at":now_iso(),"summary":summary,"items":items}
@app.get("/api/reports/errors.csv")
def error_report_csv(hours:int=Query(24,ge=1,le=720)):
    since=(datetime.now(TZ)-timedelta(hours=hours)).isoformat(); data=sanitize_csv_rows(rows("SELECT timestamp,level,service,camera_id,message FROM logs WHERE level IN ('WARNING','ERROR','CRITICAL') AND timestamp>=? ORDER BY id DESC",(since,)))
    out=io.StringIO(); fields=['timestamp','level','service','camera_id','message']; w=csv.DictWriter(out,fieldnames=fields); w.writeheader(); w.writerows(data)
    return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=zmk-error-report.csv"})


@app.get("/api/reports/support.zip")
def support_bundle(hours:int=Query(24,ge=1,le=720)):
    """Download a secret-free diagnostic package for an operator or support team.

    It deliberately exports health, safe camera state, statistics and error
    counts only; RTSP URLs, API keys, bot tokens, emails and raw log messages
    are excluded from the package.
    """
    # Do not make a support download wait for DNS/TCP probes on every RTSP
    # endpoint. The package captures the safe live camera state; an operator
    # can still run the full TCP diagnostic explicitly in the UI.
    health=system_health_data()
    camera_state=cameras()
    analytics=build_overview_analytics(hours)
    errors=error_report(hours)
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        bundle.writestr("system-health.json",json.dumps(health,ensure_ascii=False,indent=2).encode())
        bundle.writestr("camera-status.json",json.dumps({"generated_at":now_iso(),"worker":health.get("worker"),"cameras":camera_state},ensure_ascii=False,indent=2).encode())
        bundle.writestr("analytics.json",json.dumps(analytics,ensure_ascii=False,indent=2).encode())
        bundle.writestr("error-summary.json",json.dumps({"period_hours":hours,"generated_at":errors.get("generated_at"),"summary":errors.get("summary",{}),"count":len(errors.get("items",[]))},ensure_ascii=False,indent=2).encode())
        bundle.writestr("README.txt",("ZMK Vision — пакет диагностики\n\n"
            "Внутри: состояние ресурсов, безопасное состояние камер, аналитика и сводка ошибок.\n"
            "Пакет не содержит RTSP URL, пароли, API-ключи, токены ботов, email и тексты журналов.\n"
            f"Период аналитики: {hours} ч. Сформировано: {now_iso()}.\n").encode())
    return StreamingResponse(iter([archive.getvalue()]),media_type="application/zip",headers={"Content-Disposition":"attachment; filename=zmk-support-bundle.zip"})
# Search is intentionally kept local and transparent: it never queries or exposes
# RTSP URLs/artifact locations, but it understands the words operators use in
# Russian and English.  SQLite LIKE is case-sensitive for Cyrillic on many
# builds, so ranking happens after a bounded local read with casefolded text.
_SEARCH_KIND_ALIASES: dict[str, tuple[str, ...]] = {
    "camera": ("camera", "cameras", "cam", "камера", "камеры", "видео", "поток", "rtsp"),
    "event": ("event", "events", "alert", "событие", "события", "нарушение", "нарушения", "тревога"),
    "model": ("model", "models", "модель", "модели", "ai", "ии"),
    "dataset": ("dataset", "datasets", "датасет", "датасеты", "набор", "разметка"),
    "training": ("training", "train", "job", "обучение", "задача", "задачи"),
}
_SEARCH_EVENT_LABELS: dict[str, str] = {
    "no_helmet": "Без каски",
    "no_vest": "Без жилета",
    "phone_usage": "Использование телефона",
    "smoking": "Курение",
    "restricted_zone": "Опасная зона",
    "immobility": "Неподвижность",
}
_SEARCH_EVENT_ALIASES: dict[str, tuple[str, ...]] = {
    "no_helmet": ("без каски", "каска", "каски", "каску", "каске", "helmet", "hard hat", "no helmet"),
    "no_vest": ("без жилета", "жилет", "жилеты", "жилета", "жилету", "vest", "no vest"),
    "phone_usage": ("телефон", "телефона", "phone", "mobile"),
    "smoking": ("курение", "курит", "сигарета", "smoking"),
    "restricted_zone": ("опасная зона", "запретная зона", "проход", "restricted zone"),
    "immobility": ("неподвижность", "неподвижен", "лежит", "immobility"),
}
_SEARCH_SEVERITY_ALIASES: dict[str, tuple[str, ...]] = {
    "critical": ("critical", "критический", "критические", "критично"),
    "high": ("high", "высокий", "высокие"),
    "medium": ("medium", "средний", "средние"),
    "low": ("low", "низкий", "низкие"),
}
_SEARCH_REVIEW_ALIASES: dict[str, tuple[str, ...]] = {
    "pending": ("pending", "ожидает", "ожидают", "требует внимания", "новое"),
    "accepted": ("accepted", "принято", "приняты", "подтверждено", "проверено"),
    "rejected": ("rejected", "не принято", "отклонено", "ложное", "ложные"),
}
_SEARCH_CAMERA_STATUS_ALIASES: dict[str, tuple[str, ...]] = {
    "online": ("online", "онлайн", "в эфире", "работает"),
    "offline": ("offline", "офлайн", "недоступна", "не в сети"),
    "connecting": ("connecting", "подключение", "подключается"),
    "recovering": ("recovering", "восстановление", "переподключение"),
    "error": ("error", "ошибка"),
    "unknown": ("unknown", "неизвестно"),
}


def _search_normalize(value: Any) -> str:
    """Casefold Russian/English text, preserving only searchable word tokens."""
    raw = str(value or "").casefold().replace("ё", "е").replace("_", " ").replace("-", " ")
    return " ".join(re.findall(r"[\w]+", raw, flags=re.UNICODE))


def _search_alias_text(value: str, aliases: dict[str, tuple[str, ...]]) -> str:
    return " ".join((value, *aliases.get(value, ())))


def _search_parse_query(raw: str) -> tuple[str, set[str] | None]:
    """Support ergonomic prefixes such as camera:gate and модель:yolo."""
    normalized = _search_normalize(raw)
    if not normalized:
        return "", None
    alias_to_kind = {alias: kind for kind, aliases in _SEARCH_KIND_ALIASES.items() for alias in aliases}
    match = re.match(r"^\s*([^:\s]+)\s*:\s*(.*)$", raw.casefold())
    if match:
        prefix = _search_normalize(match.group(1))
        kind = alias_to_kind.get(prefix)
        if kind:
            return _search_normalize(match.group(2)), {kind}
    tokens = normalized.split()
    if len(tokens) > 1 and tokens[0] in alias_to_kind:
        return " ".join(tokens[1:]), {alias_to_kind[tokens[0]]}
    return normalized, None


def _search_match(query: str, fields: list[tuple[str, Any]]) -> tuple[int, list[str]] | None:
    """Return a deterministic relevance score or None if all query words miss.

    Exact words and prefixes rank first.  A conservative SequenceMatcher fallback
    makes one small typo (for example "касска") usable without pretending that an
    unrelated record is a match.
    """
    tokens = [token for token in _search_normalize(query).split() if token]
    normalized_fields = [(label, _search_normalize(value)) for label, value in fields if _search_normalize(value)]
    document = " ".join(value for _, value in normalized_fields)
    if not document:
        return None
    if not tokens:
        return 1, []
    words = tuple(dict.fromkeys(document.split()))
    score = 0
    for token in tokens:
        if token in words:
            score += 110
        elif any(word.startswith(token) for word in words):
            score += 82
        elif token in document:
            score += 58
        elif len(token) >= 3:
            nearby = (word for word in words if abs(len(word) - len(token)) <= max(2, len(token) // 3))
            similarity = max((SequenceMatcher(None, token, word).ratio() for word in nearby), default=0.0)
            if similarity >= 0.82:
                score += int(28 + similarity * 42)
            else:
                return None
        else:
            return None
    phrase = " ".join(tokens)
    if phrase and phrase in document:
        score += 90
    matches = [label for label, value in normalized_fields if phrase in value or any(token in value for token in tokens)]
    return score, matches[:2] or ["похожее совпадение"]


def _search_subtitle_status(value: str, aliases: dict[str, tuple[str, ...]]) -> str:
    labels = {
        "online": "Онлайн", "offline": "Офлайн", "connecting": "Подключение", "recovering": "Восстановление", "error": "Ошибка", "unknown": "Неизвестно",
        "critical": "Критический", "high": "Высокий", "medium": "Средний", "low": "Низкий",
        "pending": "Требует внимания", "accepted": "Принято", "rejected": "Не принято",
    }
    return labels.get(value, value)


@app.get("/api/search")
def global_search(q: str = Query(min_length=1, max_length=100), limit: int = Query(20, ge=1, le=50)):
    query, requested_kinds = _search_parse_query(q)
    # A prefix alone (for example "camera:") is a useful browse command.
    # Unprefixed punctuation-only input, however, has no meaningful result.
    if not query and requested_kinds is None:
        return {"query": q, "normalized_query": "", "results": []}
    con = db()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    kind_rank = {"camera": 0, "event": 1, "model": 2, "dataset": 3, "training": 4}

    def add(kind: str, item_id: str | int, title: str, subtitle: str, fields: list[tuple[str, Any]]) -> None:
        if requested_kinds is not None and kind not in requested_kinds:
            return
        # Kind words make broad queries like "камера" or "модель" useful even
        # when the record itself has an arbitrary operator-assigned name.
        kind_words = " ".join(_SEARCH_KIND_ALIASES.get(kind, (kind,)))
        match = _search_match(query, [("категория", kind_words), *fields])
        if match is None:
            return
        score, matches = match
        candidates.append((score, kind_rank[kind], {"kind": kind, "id": item_id, "title": title, "subtitle": subtitle, "matches": matches}))

    try:
        for row in con.execute("SELECT id,name,zone,description,status,fps FROM cameras ORDER BY updated_at DESC,id LIMIT 1000").fetchall():
            status = str(row[4] or "unknown")
            add("camera", row[0], row[1], f"{row[2] or 'Без зоны'} · {_search_subtitle_status(status, _SEARCH_CAMERA_STATUS_ALIASES)} · {float(row[5] or 0):.1f} FPS", [
                ("название", row[1]), ("ID камеры", row[0]), ("зона", row[2]), ("описание", row[3]), ("статус", _search_alias_text(status, _SEARCH_CAMERA_STATUS_ALIASES)),
            ])
        for row in con.execute("""SELECT e.id,e.type,e.camera_id,e.severity,e.timestamp,e.person_id,e.note,e.review_status,c.name,c.zone
            FROM events e LEFT JOIN cameras c ON c.id=e.camera_id ORDER BY e.timestamp DESC LIMIT 1200""").fetchall():
            event_type = str(row[1] or "")
            severity = str(row[3] or "")
            review = str(row[7] or "pending")
            label = _SEARCH_EVENT_LABELS.get(event_type, event_type.replace("_", " ") or "Событие")
            camera_name = str(row[8] or row[2])
            add("event", int(row[0]), label, f"{camera_name} · {_search_subtitle_status(severity, _SEARCH_SEVERITY_ALIASES)} · {_search_subtitle_status(review, _SEARCH_REVIEW_ALIASES)} · {row[4]}", [
                ("тип события", _search_alias_text(event_type, _SEARCH_EVENT_ALIASES)), ("камера", camera_name), ("ID камеры", row[2]), ("зона", row[9]),
                ("критичность", _search_alias_text(severity, _SEARCH_SEVERITY_ALIASES)), ("решение", _search_alias_text(review, _SEARCH_REVIEW_ALIASES)),
                ("объект", row[5]), ("комментарий", row[6]), ("ID события", row[0]),
            ])
        active_row = con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()
        active_model_name = str(active_row[0]) if active_row else ""
        for row in con.execute("SELECT name,format,status,source,precision,recall FROM model_registry ORDER BY id DESC LIMIT 500").fetchall():
            is_active = active_model_name == str(row[0])
            subtitle = f"{row[1]} · {row[2]}" + (" · активна" if is_active else "")
            add("model", row[0], row[0], subtitle, [
                ("название", row[0]), ("формат", row[1]), ("статус", row[2]), ("источник", row[3]), ("активность", "активная используется" if is_active else "не активна"),
            ])
        for row in con.execute("SELECT id,name,kind,image_count,media_count,class_count FROM datasets ORDER BY id DESC LIMIT 500").fetchall():
            amount = row[4] if str(row[2]) == "videos" else row[3]
            add("dataset", int(row[0]), row[1], f"{row[2]} · {amount} кадров · {row[5]} классов", [
                ("название", row[1]), ("тип", row[2]), ("кадры", amount), ("классы", row[5]),
            ])
        for row in con.execute("SELECT id,target_name,camera_id,dataset_name,status,stage,progress FROM training_jobs ORDER BY id DESC LIMIT 30").fetchall():
            source = row[3] or row[2]
            add("training", int(row[0]), row[1], f"{row[4]} · {row[6]}% · {row[5] or source}", [
                ("модель", row[1]), ("камера или датасет", source), ("статус", row[4]), ("этап", row[5]), ("ID задачи", row[0]),
            ])
    finally:
        con.close()
    candidates.sort(key=lambda item: (-item[0], item[1], _search_normalize(item[2]["title"])))
    results = [item for _, _, item in candidates[:limit]]
    return {"query": q, "normalized_query": query, "results": results}

def gpu_metrics():
    try:
        pynvml.nvmlInit(); handle=pynvml.nvmlDeviceGetHandleByIndex(0); util=pynvml.nvmlDeviceGetUtilizationRates(handle); memory=pynvml.nvmlDeviceGetMemoryInfo(handle); temperature=pynvml.nvmlDeviceGetTemperature(handle,pynvml.NVML_TEMPERATURE_GPU)
        return {"gpu":round(float(util.gpu),1),"vram":round(memory.used/memory.total*100,1) if memory.total else 0,"gpu_temp":round(float(temperature),1),"available":True}
    except pynvml.NVMLError: return {"gpu":None,"vram":None,"gpu_temp":None,"available":False}
    finally:
        try: pynvml.nvmlShutdown()
        except pynvml.NVMLError: pass

def system_health_data():
    gpu=gpu_metrics(); con=db(); con.execute("SELECT 1").fetchone(); camera_count=con.execute("SELECT COUNT(*) FROM cameras WHERE enabled=1").fetchone()[0]
    settings=_bot_settings_map(con)
    runtime={row[0]:row for row in con.execute("SELECT provider,status,updated_at FROM bot_runtime").fetchall()}
    con.close()
    snap_dir=SNAPSHOT_DIR or (DB_PATH.parent/"snapshots")
    fresh=sum(1 for r in snap_dir.glob("*.jpg") if (time.time()-r.stat().st_mtime)<10) if snap_dir.exists() else 0
    worker=inference_worker_state()
    if not camera_count: inference_status="not_configured"
    elif not worker["connected"]: inference_status="error"
    elif fresh: inference_status="healthy"
    else: inference_status="degraded"
    bot_services=[]
    for provider in BOT_PROVIDERS:
        row=runtime.get(provider); age=timestamp_age_seconds(row[2]) if row else None
        enabled=settings.get(f"{provider}_bot_enabled")=="true"
        status="healthy" if enabled and row and row[1]=="active" and age is not None and age<=20 else "disabled" if not enabled else "degraded"
        bot_services.append({"name":f"bot-{provider}","status":status})
    return {"cpu":round(psutil.cpu_percent(interval=.05),1),"ram":round(psutil.virtual_memory().percent,1),"disk":round(psutil.disk_usage(str(DB_PATH.parent)).percent,1),**gpu,"messenger_provider":_active_bot_provider(settings),"worker":worker,"services":[{"name":"api","status":"healthy"},{"name":"database","status":"healthy"},{"name":"ingestion","status":"healthy" if camera_count else "not_configured"},{"name":"inference","status":inference_status},*bot_services]}

@app.get("/api/system-health")
def system_health(): return system_health_data()
def csv_safe(value:Any):
    """Prevent spreadsheet formula injection in exported operator-controlled fields."""
    if isinstance(value,str) and value.startswith(("=","+","-","@","\t","\r")): return "'"+value
    return value

def sanitize_csv_rows(data:list[dict[str,Any]]): return [{k:csv_safe(v) for k,v in row.items()} for row in data]

_EVENT_REPORT_FIELDS=(
    "№ события","Дата и время","Тип нарушения","Код нарушения","Критичность","Уверенность, %",
    "Камера","ID камеры","Зона","Объект / человек","ID детекции","Статус проверки","Подтверждено",
    "Время решения","Комментарий оператора","Кадр нарушения","Файл кадра","Ссылка на кадр",
)
_EVENT_SEVERITY_LABELS={"critical":"Критический","high":"Высокий","medium":"Средний","low":"Низкий"}


def _event_report_rows(severity:str|None,event_type:str|None,acknowledged:bool|None,review_status:str|None,camera_id:str|None,q:str|None,hours:int|None=None) -> list[dict[str,Any]]:
    """Return exactly the event slice selected in the operator workspace."""
    ack=int(acknowledged) if acknowledged is not None else None
    term=(q or "").strip()
    like=f"%{term}%" if term else None
    since=(datetime.now(TZ)-timedelta(hours=hours)).isoformat() if hours else None
    return rows("""SELECT e.id,e.timestamp,e.camera_id,e.type,e.severity,e.confidence,e.person_id,e.external_id,e.acknowledged,e.review_status,e.reviewed_at,e.note,
        c.name AS camera_name,c.zone AS camera_zone FROM events e
        LEFT JOIN cameras c ON c.id=e.camera_id
        WHERE (? IS NULL OR e.severity=?) AND (? IS NULL OR e.type=?) AND (? IS NULL OR e.acknowledged=?) AND (? IS NULL OR e.review_status=?) AND (? IS NULL OR e.camera_id=?)
          AND (? IS NULL OR e.timestamp>=?)
          AND (? IS NULL OR e.camera_id LIKE ? OR e.person_id LIKE ? OR e.external_id LIKE ? OR e.type LIKE ? OR c.name LIKE ? OR c.zone LIKE ?)
        ORDER BY e.timestamp DESC""",(severity,severity,event_type,event_type,ack,ack,review_status,review_status,camera_id,camera_id,since,since,like,like,like,like,like,like,like))


def _event_review_state(row:dict[str,Any]) -> str:
    state=str(row.get("review_status") or "")
    if state in REVIEW_LABELS:
        return state
    return "accepted" if bool(row.get("acknowledged")) else "pending"


def _event_report_record(row:dict[str,Any]) -> dict[str,Any]:
    event_id=int(row["id"])
    frame=event_frame_path_for(event_id)
    has_frame=frame.is_file()
    review=_event_review_state(row)
    frame_file=f"frames/event-{event_id}.jpg" if has_frame else ""
    return {
        "№ события":event_id,
        "Дата и время":str(row.get("timestamp") or ""),
        "Тип нарушения":EVENT_LABELS.get(str(row.get("type") or ""),str(row.get("type") or "—")),
        "Код нарушения":str(row.get("type") or ""),
        "Критичность":_EVENT_SEVERITY_LABELS.get(str(row.get("severity") or ""),str(row.get("severity") or "—")),
        "Уверенность, %":round(float(row.get("confidence") or 0)*100,2),
        "Камера":str(row.get("camera_name") or row.get("camera_id") or "—"),
        "ID камеры":str(row.get("camera_id") or ""),
        "Зона":str(row.get("camera_zone") or "—"),
        "Объект / человек":str(row.get("person_id") or "—"),
        "ID детекции":str(row.get("external_id") or "—"),
        "Статус проверки":REVIEW_LABELS.get(review,review),
        "Подтверждено":"Да" if bool(row.get("acknowledged")) else "Нет",
        "Время решения":str(row.get("reviewed_at") or "—"),
        "Комментарий оператора":str(row.get("note") or "—"),
        "Кадр нарушения":"Есть" if has_frame else "Не сохранён",
        "Файл кадра":frame_file or "—",
        "Ссылка на кадр":f"/api/events/{event_id}/frame" if has_frame else "—",
    }


def _event_report_csv(records:list[dict[str,Any]]) -> str:
    out=io.StringIO()
    # UTF-8 BOM + semicolon delimiter open correctly in Russian Excel without a
    # manual import wizard, while the headers remain meaningful to operators.
    out.write("\ufeff")
    writer=csv.DictWriter(out,fieldnames=_EVENT_REPORT_FIELDS,delimiter=";",lineterminator="\n")
    writer.writeheader(); writer.writerows(sanitize_csv_rows(records))
    return out.getvalue()


def _event_report_html(records:list[dict[str,Any]]) -> str:
    rows_html=[]
    for record in records:
        cells=[]
        for field in _EVENT_REPORT_FIELDS:
            value=record.get(field,"—")
            if field=="Кадр нарушения" and record.get("Файл кадра") not in {"", "—"}:
                image=html_escape(str(record["Файл кадра"]),quote=True)
                cells.append(f'<td><img src="{image}" alt="Кадр нарушения события {html_escape(str(record["№ события"]))}"><small>{html_escape(str(value))}</small></td>')
            else:
                cells.append(f"<td>{html_escape(str(value))}</td>")
        rows_html.append("<tr>"+"".join(cells)+"</tr>")
    headers="".join(f"<th>{html_escape(field)}</th>" for field in _EVENT_REPORT_FIELDS)
    evidence=sum(1 for record in records if record.get("Файл кадра") not in {"", "—"})
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><title>ZMK Vision — журнал нарушений</title>
<style>body{{font:13px/1.4 Arial,sans-serif;color:#17211d;margin:24px}}h1{{margin:0 0 4px}}p{{color:#526158}}.summary{{display:flex;gap:12px;margin:18px 0}}.summary span{{padding:8px 10px;border:1px solid #d9e5dd;border-radius:8px;background:#f4faf6}}table{{width:100%;border-collapse:collapse;font-size:11px}}th{{position:sticky;top:0;background:#193426;color:#f4ffef}}th,td{{border:1px solid #dce6df;padding:6px;text-align:left;vertical-align:top}}tr:nth-child(even){{background:#f7faf8}}img{{display:block;max-width:180px;max-height:112px;border-radius:4px;background:#16231d}}td small{{display:block;margin-top:3px;color:#66756d}}@media print{{body{{margin:8px}}th{{position:static}}}}</style>
<h1>ZMK Vision — журнал нарушений</h1><p>Сформировано: {html_escape(now_iso())}. В архиве сохранены доступные кадры нарушений.</p>
<div class="summary"><span>Событий: <b>{len(records)}</b></span><span>Кадров: <b>{evidence}</b></span></div>
<table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows_html) or '<tr><td colspan="18">Событий по выбранному фильтру нет.</td></tr>'}</tbody></table></html>"""


def _event_report_args(severity:str|None,event_type:str|None,acknowledged:bool|None,review_status:Literal["pending","accepted","rejected"]|None,camera_id:str|None,q:str|None,hours:int|None=None) -> list[dict[str,Any]]:
    return [_event_report_record(row) for row in _event_report_rows(severity,event_type,acknowledged,review_status,camera_id,q,hours)]


@app.get("/api/reports/events.csv")
def report_csv(severity:str|None=None,event_type:str|None=None,acknowledged:bool|None=None,review_status:Literal["pending","accepted","rejected"]|None=None,camera_id:str|None=None,q:str|None=Query(default=None,max_length=100),hours:int|None=Query(default=None,ge=1,le=2160)):
    """Russian Excel-friendly event table with all audit fields and frame status."""
    content=_event_report_csv(_event_report_args(severity,event_type,acknowledged,review_status,camera_id,q,hours))
    return StreamingResponse(iter([content]),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=zmk-events-ru.csv"})


@app.get("/api/reports/events.zip")
def report_zip(severity:str|None=None,event_type:str|None=None,acknowledged:bool|None=None,review_status:Literal["pending","accepted","rejected"]|None=None,camera_id:str|None=None,q:str|None=Query(default=None,max_length=100),hours:int|None=Query(default=None,ge=1,le=2160)):
    """Export a ready-to-open Russian report with the table and evidence JPEGs."""
    records=_event_report_args(severity,event_type,acknowledged,review_status,camera_id,q,hours)
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        bundle.writestr("events_ru.csv",_event_report_csv(records).encode("utf-8"))
        bundle.writestr("report.html",_event_report_html(records).encode("utf-8"))
        frame_count=0
        for record in records:
            filename=str(record.get("Файл кадра") or "")
            if not filename or filename=="—":
                continue
            frame=event_frame_path_for(int(record["№ события"]))
            if frame.is_file():
                bundle.write(frame,filename)
                frame_count+=1
        bundle.writestr("README.txt",("ZMK Vision — экспорт журнала нарушений\n\n"
            "events_ru.csv — таблица с русскими названиями столбцов для Excel.\n"
            "report.html — наглядный отчёт с кадрами нарушений.\n"
            "frames/ — JPEG-кадры, доступные на момент экспорта.\n"
            f"Событий: {len(records)}; кадров: {frame_count}.\n").encode())
        bundle.writestr("manifest.json",json.dumps({"generated_at":now_iso(),"events":len(records),"frames":frame_count,"format":"zmk-event-evidence-v1"},ensure_ascii=False,indent=2).encode("utf-8"))
    return StreamingResponse(iter([archive.getvalue()]),media_type="application/zip",headers={"Content-Disposition":"attachment; filename=zmk-events-with-evidence.zip"})
@app.get("/api/stream")
async def stream():
    async def generate():
        while True:
            yield f"data: {json.dumps({'time':now_iso(),**gpu_metrics()},ensure_ascii=False)}\n\n"; await asyncio.sleep(3)
    return StreamingResponse(generate(),media_type="text/event-stream",headers={"X-Accel-Buffering":"no","Cache-Control":"no-cache"})
