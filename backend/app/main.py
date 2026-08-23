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
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

import httpx
import psutil
import pynvml
import yaml
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2.12.0"
TZ = timezone(timedelta(hours=7))
CAMERA_TELEMETRY_STALE_SECONDS = 30
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "")) if os.getenv("SNAPSHOT_DIR") else None
DB_PATH = Path(os.getenv("VIDEOANALYTICS_DB", str(Path(__file__).resolve().parent.parent / "videoanalytics.db")))
STARTED = time.time()
API_KEY = os.getenv("ZMK_API_KEY", "").strip()
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
MESSENGER_PROVIDER = os.getenv("MESSENGER_PROVIDER", "none").lower()
if MESSENGER_PROVIDER not in {"none", "telegram", "max"}: MESSENGER_PROVIDER = "none"
TRAINING_WORKER_URL = os.getenv("TRAINING_WORKER_URL", "").rstrip("/")
DATASET_DIR = Path(os.getenv("DATASET_DIR", "")) if os.getenv("DATASET_DIR") else (DB_PATH.parent / "datasets")
MODEL_DIR = Path(os.getenv("MODEL_DIR", "")) if os.getenv("MODEL_DIR") else (DB_PATH.parent / "models")
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

def apply_retention(con:sqlite3.Connection|None=None):
    own=con is None; connection=con or db()
    row=connection.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()
    if row:
        try: days=max(1,min(3650,int(float(row[0]))))
        except ValueError: days=90
        cutoff=(datetime.now(TZ)-timedelta(days=days)).isoformat()
        connection.execute("DELETE FROM events WHERE timestamp<?",(cutoff,))
        connection.execute("DELETE FROM logs WHERE timestamp<?",(cutoff,))
    if own: connection.commit(); connection.close()

def init_db():
    con = db()
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY, name TEXT NOT NULL, zone TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', rtsp_url TEXT NOT NULL DEFAULT '', fps_limit REAL NOT NULL DEFAULT 8, status TEXT NOT NULL DEFAULT 'unknown', fps REAL NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, telemetry_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '', restart_requested_at TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, camera_id TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, confidence REAL NOT NULL, person_id TEXT, external_id TEXT, acknowledged INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', FOREIGN KEY(camera_id) REFERENCES cameras(id));
    CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, level TEXT NOT NULL, service TEXT NOT NULL, message TEXT NOT NULL, camera_id TEXT);
    CREATE TABLE IF NOT EXISTS worker_status(name TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', camera_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS model_registry(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, format TEXT NOT NULL, status TEXT NOT NULL, precision REAL, recall REAL, trained_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'external', artifact_uri TEXT NOT NULL DEFAULT '', checksum TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS training_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, camera_id TEXT NOT NULL, base_model TEXT NOT NULL, target_name TEXT NOT NULL, image_count INTEGER NOT NULL, epochs INTEGER NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL, error TEXT, batch INTEGER NOT NULL DEFAULT 8, imgsz INTEGER NOT NULL DEFAULT 640, patience INTEGER NOT NULL DEFAULT 20, confidence REAL NOT NULL DEFAULT .35, val_split REAL NOT NULL DEFAULT .2, capture_fps REAL NOT NULL DEFAULT 2, FOREIGN KEY(camera_id) REFERENCES cameras(id));
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, login TEXT UNIQUE NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
    """)
    camera_columns={r[1] for r in con.execute("PRAGMA table_info(cameras)").fetchall()}
    for column,ddl in {"description":"TEXT NOT NULL DEFAULT ''","fps_limit":"REAL NOT NULL DEFAULT 8","created_at":"TEXT NOT NULL DEFAULT ''","telemetry_at":"TEXT NOT NULL DEFAULT ''","last_error":"TEXT NOT NULL DEFAULT ''","restart_requested_at":"TEXT NOT NULL DEFAULT ''"}.items():
        if column not in camera_columns: con.execute(f"ALTER TABLE cameras ADD COLUMN {column} {ddl}")
    con.execute("UPDATE cameras SET created_at=updated_at WHERE created_at='' OR created_at IS NULL")
    # A live MJPEG browser stream has a deliberate upper bound: keep stored
    # legacy values consistent with the UI/API maximum of 20 FPS.
    con.execute("UPDATE cameras SET fps_limit=20 WHERE fps_limit>20")
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
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_events_external_id ON events(external_id) WHERE external_id IS NOT NULL")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_timestamp ON events(timestamp DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_camera_timestamp ON events(camera_id,timestamp DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_severity_ack ON events(severity,acknowledged)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_logs_timestamp_level ON logs(timestamp DESC,level)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_training_status ON training_jobs(status,created_at DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_capture_jobs_status ON dataset_capture_jobs(status,created_at DESC)")
    con.execute("UPDATE training_jobs SET status='failed',stage='Прервано перезапуском',error='Worker restarted before completion',updated_at=? WHERE status IN ('queued','running')",(now_iso(),))
    config_defaults={
        "active_model":"", "active_model_disabled":"false", "ppe_trial_previous_model":"", "site_name":"ZMK Vision", "timezone":"Asia/Krasnoyarsk", "language":"ru",
        "retention_days":"90", "archive_quality":"90", "archive_clip_seconds":"10",
        "inference_fps":"8", "inference_device":"cuda:0", "batch_size":"4", "nms_iou":"0.45",
        "helmet_conf":"0.85", "vest_conf":"0.80", "phone_conf":"0.78", "smoking_conf":"0.80", "restricted_zone_conf":"0.82", "immobility_conf":"0.80", "min_model_precision":"90", "min_model_recall":"85",
        "telegram_enabled":"false", "telegram_chat_ids":"", "critical_alerts":"true",
        "webhook_enabled":"false", "webhook_url":"", "webhook_timeout":"5",
        "minio_endpoint":"minio:9000", "minio_bucket":"videoanalytics", "minio_secure":"false",
        "rtsp_reconnect_seconds":"5", "event_cooldown_seconds":"30"
    }
    for key,value in config_defaults.items(): con.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",(key,value))
    disabled_row=con.execute("SELECT value FROM settings WHERE key='active_model_disabled'").fetchone()
    if disabled_row and disabled_row[0]=='true':
        # No model is active after an explicitly stopped PPE trial. Do not
        # resurrect a ready model merely because the API/container restarted.
        con.execute("UPDATE settings SET value='' WHERE key='active_model'")
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
                if fallback: con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","model_manager",f"Active model repaired to {fallback[0]}"))
    bootstrap_env_camera(con)
    apply_retention(con)
    con.commit(); con.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try: yield
    finally:
        for task in [*list(_training_tasks.values()),*list(_dataset_capture_tasks.values())]: task.cancel()
        pending=[*list(_training_tasks.values()),*list(_dataset_capture_tasks.values())]
        if pending: await asyncio.gather(*pending,return_exceptions=True)

app=FastAPI(title="ZMK Vision API",version=APP_VERSION,description="On-premise API контура видеоаналитики",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[x.strip() for x in os.getenv("CORS_ORIGINS","http://localhost:5173").split(",") if x.strip()],allow_credentials=False,allow_methods=["GET","POST","PUT","PATCH","DELETE"],allow_headers=["Content-Type","X-API-Key","X-Telegram-Init-Data"])

def custom_openapi():
    if app.openapi_schema: return app.openapi_schema
    schema=get_openapi(title=app.title,version=app.version,description=app.description,routes=app.routes)
    schema.setdefault("components",{}).setdefault("securitySchemes",{})["ApiKeyAuth"]={"type":"apiKey","in":"header","name":"X-API-Key"}
    for path,methods in schema.get("paths",{}).items():
        if path.startswith("/api/") and path!="/api/health":
            for operation in methods.values():
                if isinstance(operation,dict): operation["security"]=[{"ApiKeyAuth":[]}]
    app.openapi_schema=schema; return schema
app.openapi=custom_openapi

def telegram_webapp_role(init_data:str)->str|None:
    """Validate Telegram Mini App initData and return the whitelisted role."""
    if not TELEGRAM_BOT_TOKEN or not init_data or len(init_data)>8192: return None
    try:
        values=dict(parse_qsl(init_data,keep_blank_values=True)); supplied=values.pop("hash","")
        auth_date=int(values.get("auth_date","0"))
        if abs(int(time.time())-auth_date)>3600: return None
        check="\n".join(f"{k}={v}" for k,v in sorted(values.items()))
        secret=hmac.new(b"WebAppData",TELEGRAM_BOT_TOKEN.encode(),hashlib.sha256).digest()
        expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied,expected): return None
        user=json.loads(values.get("user","{}")); return TELEGRAM_ROLES.get(int(user.get("id",0)))
    except (ValueError,TypeError,json.JSONDecodeError): return None

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Optional API-key protection, size/rate limits and baseline response headers."""
    path=request.url.path
    public=path in {"/api/health","/docs","/openapi.json","/redoc"} or not path.startswith("/api/")
    if path.startswith("/api/internal/"):
        from fastapi.responses import JSONResponse
        # A worker token is auto-provisioned on the shared model-data volume.
        # We only 503 if it could not be provisioned at all (e.g. volume
        # read-only). Otherwise we require it strictly (constant-time).
        if not WORKER_TOKEN:
            return JSONResponse({"detail":"Worker token could not be provisioned: ensure model-data volume is writable"},status_code=503)
        if not hmac.compare_digest(request.headers.get("X-Worker-Token",""),WORKER_TOKEN): return JSONResponse({"detail":"Invalid worker token"},status_code=401)
    api_key_ok=bool(API_KEY and hmac.compare_digest(request.headers.get("X-API-Key",""),API_KEY))
    telegram_role=telegram_webapp_role(request.headers.get("X-Telegram-Init-Data",""))
    if API_KEY and not public and not (api_key_ok or telegram_role or path.startswith("/api/internal/")):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail":"Invalid or missing API credentials"},status_code=401,headers={"WWW-Authenticate":"ApiKey"})
    if telegram_role and not api_key_ok:
        admin_write=path.startswith(("/api/admin/","/api/training/","/api/settings","/api/models/")) and request.method!="GET"
        admin_read=path.startswith(("/api/admin/","/api/logs","/api/settings"))
        operator_only=path.startswith("/api/reports/")
        viewer_write=telegram_role=="viewer" and request.method!="GET"
        operator_write=telegram_role=="operator" and request.method!="GET" and not (path.startswith("/api/events/") and path.endswith("/ack"))
        if (telegram_role!="admin" and (admin_write or admin_read)) or (telegram_role=="viewer" and operator_only) or viewer_write or operator_write:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail":"Insufficient Telegram role"},status_code=403)
    length=request.headers.get("content-length")
    # Dataset uploads carry a local zip archive, commonly larger than the
    # JSON cap, so allow a generous size on that path only.
    cap=512_000_000 if path.startswith("/api/datasets") and request.method=="POST" else 2_000_000
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
            (camera_id, "Камера 01", "Без зоны", "Добавлена из RTSP_CAM_01", rtsp_url, 8, "connecting", 0, 0, 1, timestamp, timestamp, "", timestamp),
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
    fps_limit:float=Field(default=8,ge=.1,le=20)
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
    fps_limit:float=Field(default=8,ge=.1,le=20)
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
class CameraSnapshotIn(BaseModel):
    jpeg_base64:str=Field(min_length=16,max_length=1_800_000)
    captured_at:datetime|None=None
class SettingIn(BaseModel): value:float=Field(ge=.1,le=1)
class AckIn(BaseModel): note:str=Field(default="",max_length=500)
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
class UserIn(BaseModel):
    name:str=Field(min_length=2,max_length=80)
    login:str=Field(min_length=2,max_length=40,pattern=r"^[a-zA-Z0-9._-]+$")
    role:Literal["admin","operator","viewer"]
class ModelIn(BaseModel):
    name:str=Field(min_length=2,max_length=120,pattern=r"^[a-zA-Z0-9._-]+$")
    format:Literal["ONNX","ONNX FP16","TensorRT","TensorRT FP16","PyTorch"]
    precision:float=Field(ge=0,le=100)
    recall:float=Field(ge=0,le=100)
    source:str=Field(default="external",max_length=200)
    artifact_uri:str=Field(min_length=1,max_length=1000)
    checksum:str=Field(default="",max_length=128,pattern=r"^[a-fA-F0-9]*$")
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

def inference_worker_state() -> dict[str,Any]:
    con=db(); row=con.execute("SELECT status,detail,camera_count,updated_at FROM worker_status WHERE name='inference'").fetchone(); con.close()
    if not row: return {"connected":False,"status":"absent","detail":"Нет heartbeat от inference worker","camera_count":0,"age_seconds":None}
    age=timestamp_age_seconds(row[3]); connected=age is not None and age<=15 and row[0] in {"starting","running","idle","degraded"}
    return {"connected":connected,"status":row[0],"detail":row[1],"camera_count":row[2],"updated_at":row[3],"age_seconds":age}

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
    trend=[]
    for h in range(11,-1,-1):
        end=datetime.now(TZ)-timedelta(hours=h); start=end-timedelta(hours=1)
        n=con.execute("SELECT COUNT(*) FROM events WHERE timestamp BETWEEN ? AND ?",(start.isoformat(),end.isoformat())).fetchone()[0]
        trend.append({"label":end.strftime("%H:00"),"value":n})
    con.close(); gpu=gpu_metrics(); return {"cameras":{"total":total,"online":online},"events24h":events24,"critical_unacked":critical,"avg_fps":round(avg[0],1),"avg_latency_ms":round(avg[1]),"gpu_load":gpu["gpu"],"gpu_temp":gpu["gpu_temp"],"messenger_provider":MESSENGER_PROVIDER,"active_model":model[0] if model else None,"precision":model[1] if model else None,"recall":model[2] if model else None,"trend":trend}

@app.get("/api/internal/cameras")
def internal_cameras(): return rows("SELECT id,name,rtsp_url,fps_limit,enabled,restart_requested_at FROM cameras WHERE enabled=1 AND rtsp_url!='' ORDER BY id")

@app.post("/api/internal/inference/heartbeat",status_code=204)
def inference_heartbeat(payload:InferenceHeartbeat):
    con=db(); con.execute("INSERT INTO worker_status(name,status,detail,camera_count,updated_at) VALUES('inference',?,?,?,?) ON CONFLICT(name) DO UPDATE SET status=excluded.status,detail=excluded.detail,camera_count=excluded.camera_count,updated_at=excluded.updated_at",(payload.status,payload.detail,payload.camera_count,now_iso())); con.commit(); con.close()

@app.get("/api/internal/active-model")
def internal_active_model():
    data=rows("SELECT m.name,m.format,m.artifact_uri,m.checksum,m.source FROM model_registry m JOIN settings s ON s.key='active_model' AND s.value=m.name WHERE m.status='ready'")
    return data[0] if data else None

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
    return {"id":camera_id,"updated":True,"configured":bool(new_rtsp),"rtsp_updated":rtsp_updated,"stream_reset":stream_changed}

@app.delete("/api/cameras/{camera_id}")
def delete_camera(camera_id:str,delete_events:bool=False):
    con=db(); camera=con.execute("SELECT name FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not camera: con.close(); raise HTTPException(404,"Камера не найдена")
    event_count=con.execute("SELECT COUNT(*) FROM events WHERE camera_id=?",(camera_id,)).fetchone()[0]
    if event_count and not delete_events: con.close(); raise HTTPException(409,f"У камеры есть события: {event_count}. Подтвердите delete_events=true")
    con.execute("BEGIN IMMEDIATE")
    if delete_events: con.execute("DELETE FROM events WHERE camera_id=?",(camera_id,))
    con.execute("DELETE FROM cameras WHERE id=?",(camera_id,)); con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(now_iso(),"WARNING","camera_manager",f"Camera deleted: {camera[0]}",camera_id)); con.commit(); con.close()
    snapshot=snapshot_path_for(camera_id); snapshot.unlink(missing_ok=True); clear_live_frame(camera_id)
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
    return {"id":camera_id,"enabled":bool(enabled),"stream_reset":True}

@app.post("/api/cameras/{camera_id}/restart")
def restart_camera(camera_id:str):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404,"Камера не найдена")
    if not row[0]: con.close(); raise HTTPException(409,"Сначала включите аналитику камеры")
    timestamp=now_iso(); con.execute("UPDATE cameras SET status='connecting',fps=0,latency_ms=0,last_error='',telemetry_at='',restart_requested_at=?,updated_at=? WHERE id=?",(timestamp,timestamp,camera_id)); con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(timestamp,"INFO","camera_manager","RTSP restart requested",camera_id)); con.commit(); con.close()
    snapshot_path_for(camera_id).unlink(missing_ok=True); clear_live_frame(camera_id)
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

@app.get("/api/cameras/{camera_id}/mjpeg")
def camera_mjpeg(camera_id:str):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Камера не найдена")
    if not row[0]: raise HTTPException(409,"Аналитика камеры отключена")
    def generate():
        sequence=-1
        while True:
            with _live_frames_lock:
                item=_live_frames.get(camera_id)
            if item and item[0]!=sequence:
                sequence,_,image=item
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "+str(len(image)).encode()+b"\r\n\r\n"+image+b"\r\n"
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
    with ThreadPoolExecutor(max_workers=min(10,max(1,len(camera_ids)))) as pool: camera_results=list(pool.map(diagnose_camera_row,camera_ids))
    for result in camera_results:
        age=snapshot_age_seconds(result["camera_id"]); result["snapshot_age_seconds"]=age; result["snapshot"]="fresh" if age is not None and age<15 else ("stale" if age is not None else "none"); result["live_frame_age_seconds"]=live_frame_age_seconds(result["camera_id"])
    return {"generated_at":now_iso(),"system":system_health_data(),"worker":inference_worker_state(),"cameras":camera_results}

@app.get("/api/events")
def events(limit:int=Query(50,ge=1,le=500),severity:str|None=None,event_type:str|None=None,acknowledged:bool|None=None):
    ack=int(acknowledged) if acknowledged is not None else None
    return rows("""SELECT e.*,c.name camera_name,c.zone FROM events e JOIN cameras c ON c.id=e.camera_id
        WHERE (? IS NULL OR e.severity=?) AND (? IS NULL OR e.type=?) AND (? IS NULL OR e.acknowledged=?)
        ORDER BY e.timestamp DESC LIMIT ?""",(severity,severity,event_type,event_type,ack,ack,limit))
@app.post("/api/events/{event_id}/ack")
def ack(event_id:int,payload:AckIn):
    con=db(); cur=con.execute("UPDATE events SET acknowledged=1,note=? WHERE id=?",(payload.note,event_id)); con.commit(); con.close()
    if not cur.rowcount: raise HTTPException(404,"Событие не найдено")
    return {"id":event_id,"acknowledged":True}
@app.post("/api/inference/detections")
def ingest_detections(payload:DetectionBatch):
    """Validated contract from inference workers to the event subsystem."""
    con=db(); con.execute("BEGIN IMMEDIATE"); active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
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
            if d.model_name != active: reason=f"stale_model: active={active}"
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
    con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","inference_gateway",f"batch model={active} accepted={len(accepted)} rejected={len(rejected)}"))
    webhook={r[0]:r[1] for r in con.execute("SELECT key,value FROM settings WHERE key IN ('webhook_enabled','webhook_url','webhook_timeout')").fetchall()}; con.commit(); con.close()
    if accepted and webhook.get('webhook_enabled')=='true' and webhook.get('webhook_url'):
        try: httpx.post(webhook['webhook_url'],json={"source":"zmk-vision","model":active,"events":accepted,"timestamp":now_iso()},timeout=float(webhook.get('webhook_timeout','5'))).raise_for_status()
        except httpx.HTTPError as exc:
            logcon=db(); logcon.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"ERROR","integration",f"Webhook delivery failed: {str(exc)[:300]}")); logcon.commit(); logcon.close()
    return {"active_model":active,"accepted":accepted,"rejected":rejected,"received":len(payload.detections)}

@app.get("/api/admin/config")
def get_config():
    data={r["key"]:r["value"] for r in rows("SELECT * FROM settings")}
    groups={
      "general":["site_name","timezone","language","retention_days"],
      "inference":["inference_fps","inference_device","batch_size","nms_iou","helmet_conf","vest_conf","phone_conf","smoking_conf","restricted_zone_conf","immobility_conf","min_model_precision","min_model_recall","event_cooldown_seconds"],
      "archive":["archive_quality","archive_clip_seconds","minio_endpoint","minio_bucket","minio_secure"],
      "notifications":["telegram_enabled","telegram_chat_ids","critical_alerts"],
      "integration":["webhook_enabled","webhook_url","webhook_timeout","rtsp_reconnect_seconds"]}
    return {g:{k:data.get(k,"") for k in keys} for g,keys in groups.items()}

CONFIG_ALLOWED={"site_name","timezone","language","retention_days","inference_fps","inference_device","batch_size","nms_iou","helmet_conf","vest_conf","phone_conf","smoking_conf","restricted_zone_conf","immobility_conf","min_model_precision","min_model_recall","event_cooldown_seconds","archive_quality","archive_clip_seconds","minio_endpoint","minio_bucket","minio_secure","telegram_enabled","telegram_chat_ids","critical_alerts","webhook_enabled","webhook_url","webhook_timeout","rtsp_reconnect_seconds"}
@app.put("/api/admin/config")
def update_config(payload:ConfigPatch):
    unknown=set(payload.values)-CONFIG_ALLOWED
    if unknown: raise HTTPException(422,f"Неизвестные параметры: {', '.join(sorted(unknown))}")
    numeric={"retention_days":(1,3650),"inference_fps":(1,30),"batch_size":(1,64),"nms_iou":(.1,.95),"helmet_conf":(.1,1),"vest_conf":(.1,1),"phone_conf":(.1,1),"smoking_conf":(.1,1),"restricted_zone_conf":(.1,1),"immobility_conf":(.1,1),"min_model_precision":(0,100),"min_model_recall":(0,100),"event_cooldown_seconds":(0,3600),"archive_quality":(10,100),"archive_clip_seconds":(2,120),"webhook_timeout":(1,60),"rtsp_reconnect_seconds":(1,300)}
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


@app.get("/api/models")
def models():
    active=rows("SELECT value FROM settings WHERE key='active_model'")[0]["value"]
    limits={r["key"]:float(r["value"]) for r in rows("SELECT key,value FROM settings WHERE key IN ('min_model_precision','min_model_recall')")}
    data=rows("SELECT name,format,status,precision,recall,trained_at,source,artifact_uri,checksum FROM model_registry ORDER BY id DESC")
    for item in data:
        item["active"]=item["name"]==active
        item["trial_eligible"]=_is_trial_preset_source(item.get("source"))
        item["trial_mode"]=bool(item["active"] and item["trial_eligible"] and not _model_meets_quality(item.get("precision"),item.get("recall"),limits))
    return data


@app.post("/api/models",status_code=201)
def register_model(payload:ModelIn):
    con=db()
    try: con.execute("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)",(payload.name,payload.format,"ready",payload.precision,payload.recall,now_iso(),payload.source,payload.artifact_uri,payload.checksum)); con.commit()
    except sqlite3.IntegrityError: con.close(); raise HTTPException(409,"Модель с таким именем уже существует")
    con.close(); return {"name":payload.name,"status":"ready","registered":True}


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
    ext=".pt"
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
    return {"healthy":healthy,"trial_mode":trial_mode,"model":dict(model),"requirements":{"precision":limits.get('min_model_precision',90),"recall":limits.get('min_model_recall',85)},"last_inference":dict(last) if last else None}


def _activate_model(name:str, *, allow_trial:bool=False):
    started=time.perf_counter()
    con=db()
    try:
        model=con.execute("SELECT status,precision,recall,source FROM model_registry WHERE name=?",(name,)).fetchone()
        if not model: raise HTTPException(404,"Модель не найдена")
        if model[0] != "ready": raise HTTPException(409,"Модель ещё не готова")
        limits={r[0]:float(r[1]) for r in con.execute("SELECT key,value FROM settings WHERE key IN ('min_model_precision','min_model_recall')").fetchall()}
        quality_ok=_model_meets_quality(model[1],model[2],limits)
        trial_mode=not quality_ok and allow_trial and _is_trial_preset_source(model[3])
        if not quality_ok and not trial_mode:
            if model[1] is None or model[2] is None:
                raise HTTPException(409,"У модели отсутствуют метрики валидации. Для PPE-пресета используйте отдельную кнопку «Включить PPE-тест».")
            raise HTTPException(409,"Метрики модели ниже минимально допустимых")
        con.execute("BEGIN IMMEDIATE")
        old=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
        if old==name:
            con.execute("UPDATE settings SET value='false' WHERE key='active_model_disabled'")
            con.commit()
            return {"active_model":name,"previous_model":old,"hot_swap":False,"idempotent":True,"trial_mode":trial_mode,"control_plane_switch_ms":round((time.perf_counter()-started)*1000,2),"downtime_ms":0}
        con.execute("UPDATE settings SET value=? WHERE key='active_model'",(name,))
        # Keep a validated model selected before a PPE test so stopping the
        # test restores the exact previous state instead of leaving an
        # operator unexpectedly without analytics.
        con.execute("UPDATE settings SET value=? WHERE key='ppe_trial_previous_model'",(old if trial_mode else "",))
        con.execute("UPDATE settings SET value='false' WHERE key='active_model_disabled'")
        level="WARNING" if trial_mode else "INFO"
        mode="PPE trial" if trial_mode else "validated"
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),level,"model_manager",f"Control-plane hot-swap {old} -> {name} ({mode}) completed"))
        con.commit()
        return {"active_model":name,"previous_model":old,"hot_swap":True,"idempotent":False,"trial_mode":trial_mode,"control_plane_switch_ms":round((time.perf_counter()-started)*1000,2),"downtime_ms":0}
    finally:
        con.close()


@app.post("/api/models/{name}/activate")
def activate(name:str):
    return _activate_model(name)


@app.post("/api/models/{name}/activate-trial")
def activate_trial_model(name:str):
    """Explicitly enable only the selected PPE baseline for an on-site trial."""
    return _activate_model(name,allow_trial=True)


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
        con.execute("UPDATE settings SET value='' WHERE key='ppe_trial_previous_model'")
        con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","model_manager",f"PPE trial stopped: {name}; restored={restored or 'none'}"))
        con.commit()
        return {"active_model":restored,"restored_model":restored or None,"stopped":True,"idempotent":False}
    finally:
        con.close()

@app.delete("/api/models/{name}")
def delete_model(name:str):
    """Remove a model from the registry (and its artifact file if it is a
    locally-downloaded preset). Refuses to delete the currently active model."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,120}",name or ""): raise HTTPException(422,"Недопустимое имя модели")
    con=db(); row=con.execute("SELECT status,source,artifact_uri FROM model_registry WHERE name=?",(name,)).fetchone()
    if not row: con.close(); raise HTTPException(404,"Модель не найдена")
    active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
    if active==name: con.close(); raise HTTPException(409,"Нельзя удалить активную модель — сначала переключитесь на другую (или отключите) через 'Горячая замена'")
    jobs=con.execute("SELECT COUNT(*) FROM training_jobs WHERE target_name=? AND status IN ('queued','running')",(name,)).fetchone()[0]
    if jobs: con.close(); raise HTTPException(409,"Модель используется текущей задачей обучения")
    source=row[1]; artifact_uri=row[2] or ""
    con.execute("DELETE FROM model_registry WHERE name=?",(name,))
    con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","model_manager",f"Model deleted: {name} (source={source})"))
    con.commit(); con.close()
    removed_file=False
    if source and source.startswith("preset:") and artifact_uri.startswith("file://"):
        try:
            artifact=Path(artifact_uri.removeprefix("file://")).resolve()
            base=MODEL_DIR.resolve()
            if base.exists() and base in artifact.parents and artifact.is_file():
                artifact.unlink(); removed_file=True
        except (OSError,ValueError):
            removed_file=False
    return {"name":name,"deleted":True,"removed_artifact_file":removed_file,"source":source}

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
@app.get("/api/search")
def global_search(q:str=Query(min_length=2,max_length=100),limit:int=Query(20,ge=1,le=50)):
    term=f"%{q.strip()}%"; con=db(); results=[]
    for row in con.execute("SELECT id,name,zone,description,status FROM cameras WHERE name LIKE ? OR zone LIKE ? OR description LIKE ? LIMIT ?",(term,term,term,limit)).fetchall(): results.append({"kind":"camera","id":row[0],"title":row[1],"subtitle":f"{row[2]} · {row[4]}"})
    remaining=max(0,limit-len(results))
    if remaining:
        for row in con.execute("SELECT id,type,camera_id,severity,timestamp FROM events WHERE type LIKE ? OR person_id LIKE ? OR note LIKE ? ORDER BY timestamp DESC LIMIT ?",(term,term,term,remaining)).fetchall(): results.append({"kind":"event","id":row[0],"title":row[1],"subtitle":f"{row[2]} · {row[3]} · {row[4]}"})
    remaining=max(0,limit-len(results))
    if remaining:
        for row in con.execute("SELECT name,format,status,source FROM model_registry WHERE name LIKE ? OR source LIKE ? LIMIT ?",(term,term,remaining)).fetchall(): results.append({"kind":"model","id":row[0],"title":row[0],"subtitle":f"{row[1]} · {row[2]}"})
    con.close(); return {"query":q,"results":results}

def gpu_metrics():
    try:
        pynvml.nvmlInit(); handle=pynvml.nvmlDeviceGetHandleByIndex(0); util=pynvml.nvmlDeviceGetUtilizationRates(handle); memory=pynvml.nvmlDeviceGetMemoryInfo(handle); temperature=pynvml.nvmlDeviceGetTemperature(handle,pynvml.NVML_TEMPERATURE_GPU)
        return {"gpu":round(float(util.gpu),1),"vram":round(memory.used/memory.total*100,1) if memory.total else 0,"gpu_temp":round(float(temperature),1),"available":True}
    except pynvml.NVMLError: return {"gpu":None,"vram":None,"gpu_temp":None,"available":False}
    finally:
        try: pynvml.nvmlShutdown()
        except pynvml.NVMLError: pass

def system_health_data():
    gpu=gpu_metrics(); con=db(); con.execute("SELECT 1").fetchone(); camera_count=con.execute("SELECT COUNT(*) FROM cameras WHERE enabled=1").fetchone()[0]; con.close()
    snap_dir=SNAPSHOT_DIR or (DB_PATH.parent/"snapshots")
    fresh=sum(1 for r in snap_dir.glob("*.jpg") if (time.time()-r.stat().st_mtime)<10) if snap_dir.exists() else 0
    worker=inference_worker_state()
    if not camera_count: inference_status="not_configured"
    elif not worker["connected"]: inference_status="error"
    elif fresh: inference_status="healthy"
    else: inference_status="degraded"
    return {"cpu":round(psutil.cpu_percent(interval=.05),1),"ram":round(psutil.virtual_memory().percent,1),"disk":round(psutil.disk_usage(str(DB_PATH.parent)).percent,1),**gpu,"messenger_provider":MESSENGER_PROVIDER,"worker":worker,"services":[{"name":"api","status":"healthy"},{"name":"database","status":"healthy"},{"name":"ingestion","status":"healthy" if camera_count else "not_configured"},{"name":"inference","status":inference_status}]}

@app.get("/api/system-health")
def system_health(): return system_health_data()
def csv_safe(value:Any):
    """Prevent spreadsheet formula injection in exported operator-controlled fields."""
    if isinstance(value,str) and value.startswith(("=","+","-","@","\t","\r")): return "'"+value
    return value

def sanitize_csv_rows(data:list[dict[str,Any]]): return [{k:csv_safe(v) for k,v in row.items()} for row in data]

@app.get("/api/reports/events.csv")
def report_csv():
    data=sanitize_csv_rows(rows("SELECT timestamp,camera_id,type,severity,confidence,person_id,acknowledged,note FROM events ORDER BY timestamp DESC"))
    out=io.StringIO(); w=csv.DictWriter(out,fieldnames=data[0].keys() if data else []); w.writeheader(); w.writerows(data)
    return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=zmk-events.csv"})
@app.get("/api/stream")
async def stream():
    async def generate():
        while True:
            yield f"data: {json.dumps({'time':now_iso(),**gpu_metrics()},ensure_ascii=False)}\n\n"; await asyncio.sleep(3)
    return StreamingResponse(generate(),media_type="text/event-stream",headers={"X-Accel-Buffering":"no","Cache-Control":"no-cache"})
