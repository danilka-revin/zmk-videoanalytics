from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import socket
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

import httpx
import psutil
import pynvml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

TZ = timezone(timedelta(hours=7))
DB_PATH = Path(os.getenv("VIDEOANALYTICS_DB", str(Path(__file__).resolve().parent.parent / "videoanalytics.db")))
STARTED = time.time()
API_KEY = os.getenv("ZMK_API_KEY", "").strip()
WORKER_TOKEN = os.getenv("ZMK_WORKER_TOKEN", "").strip()
try: RATE_LIMIT_PER_MINUTE = max(10,int(os.getenv("RATE_LIMIT_PER_MINUTE", "120")))
except ValueError: RATE_LIMIT_PER_MINUTE = 120
_rate_buckets: dict[str, list[float]] = {}
_training_tasks: dict[int,asyncio.Task] = {}
MESSENGER_PROVIDER = os.getenv("MESSENGER_PROVIDER", "none").lower()
if MESSENGER_PROVIDER not in {"none", "telegram", "max"}: MESSENGER_PROVIDER = "none"
TRAINING_WORKER_URL = os.getenv("TRAINING_WORKER_URL", "").rstrip("/")
SEED_TEST_DATA = os.getenv("ZMK_SEED_TEST_DATA", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ROLES = {**{int(x):"viewer" for x in os.getenv("TELEGRAM_VIEWER_IDS","").split(",") if x.strip().isdigit()},**{int(x):"operator" for x in os.getenv("TELEGRAM_OPERATOR_IDS","").split(",") if x.strip().isdigit()},**{int(x):"admin" for x in os.getenv("TELEGRAM_ADMIN_IDS","").split(",") if x.strip().isdigit()}}

def now_iso(): return datetime.now(TZ).isoformat(timespec="seconds")
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
    CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY, name TEXT NOT NULL, zone TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', rtsp_url TEXT NOT NULL DEFAULT '', fps_limit REAL NOT NULL DEFAULT 8, status TEXT NOT NULL DEFAULT 'unknown', fps REAL NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, camera_id TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, confidence REAL NOT NULL, person_id TEXT, external_id TEXT, acknowledged INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', FOREIGN KEY(camera_id) REFERENCES cameras(id));
    CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, level TEXT NOT NULL, service TEXT NOT NULL, message TEXT NOT NULL, camera_id TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS model_registry(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, format TEXT NOT NULL, status TEXT NOT NULL, precision REAL, recall REAL, trained_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'external', artifact_uri TEXT NOT NULL DEFAULT '', checksum TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS training_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, camera_id TEXT NOT NULL, base_model TEXT NOT NULL, target_name TEXT NOT NULL, image_count INTEGER NOT NULL, epochs INTEGER NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL, error TEXT, batch INTEGER NOT NULL DEFAULT 8, imgsz INTEGER NOT NULL DEFAULT 640, patience INTEGER NOT NULL DEFAULT 20, confidence REAL NOT NULL DEFAULT .35, val_split REAL NOT NULL DEFAULT .2, capture_fps REAL NOT NULL DEFAULT 2, FOREIGN KEY(camera_id) REFERENCES cameras(id));
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, login TEXT UNIQUE NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
    """)
    camera_columns={r[1] for r in con.execute("PRAGMA table_info(cameras)").fetchall()}
    for column,ddl in {"description":"TEXT NOT NULL DEFAULT ''","fps_limit":"REAL NOT NULL DEFAULT 8","created_at":"TEXT NOT NULL DEFAULT ''"}.items():
        if column not in camera_columns: con.execute(f"ALTER TABLE cameras ADD COLUMN {column} {ddl}")
    con.execute("UPDATE cameras SET created_at=updated_at WHERE created_at='' OR created_at IS NULL")
    model_columns={r[1] for r in con.execute("PRAGMA table_info(model_registry)").fetchall()}
    for column,ddl in {"artifact_uri":"TEXT NOT NULL DEFAULT ''","checksum":"TEXT NOT NULL DEFAULT ''"}.items():
        if column not in model_columns: con.execute(f"ALTER TABLE model_registry ADD COLUMN {column} {ddl}")
    training_columns={r[1] for r in con.execute("PRAGMA table_info(training_jobs)").fetchall()}
    for column,ddl in {"batch":"INTEGER NOT NULL DEFAULT 8","imgsz":"INTEGER NOT NULL DEFAULT 640","patience":"INTEGER NOT NULL DEFAULT 20","confidence":"REAL NOT NULL DEFAULT .35","val_split":"REAL NOT NULL DEFAULT .2","capture_fps":"REAL NOT NULL DEFAULT 2"}.items():
        if column not in training_columns: con.execute(f"ALTER TABLE training_jobs ADD COLUMN {column} {ddl}")
    event_columns={r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
    if "external_id" not in event_columns: con.execute("ALTER TABLE events ADD COLUMN external_id TEXT")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_events_external_id ON events(external_id) WHERE external_id IS NOT NULL")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_timestamp ON events(timestamp DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_camera_timestamp ON events(camera_id,timestamp DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_severity_ack ON events(severity,acknowledged)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_logs_timestamp_level ON logs(timestamp DESC,level)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_training_status ON training_jobs(status,created_at DESC)")
    con.execute("UPDATE training_jobs SET status='failed',stage='Прервано перезапуском',error='Worker restarted before completion',updated_at=? WHERE status IN ('queued','running')",(now_iso(),))
    config_defaults={
        "active_model":"", "site_name":"ZMK Vision", "timezone":"Asia/Krasnoyarsk", "language":"ru",
        "retention_days":"90", "archive_quality":"90", "archive_clip_seconds":"10",
        "inference_fps":"8", "inference_device":"cuda:0", "batch_size":"4", "nms_iou":"0.45",
        "helmet_conf":"0.85", "vest_conf":"0.80", "phone_conf":"0.78", "smoking_conf":"0.80", "restricted_zone_conf":"0.82", "immobility_conf":"0.80", "min_model_precision":"90", "min_model_recall":"85",
        "telegram_enabled":"false", "telegram_chat_ids":"", "critical_alerts":"true",
        "webhook_enabled":"false", "webhook_url":"", "webhook_timeout":"5",
        "minio_endpoint":"minio:9000", "minio_bucket":"videoanalytics", "minio_secure":"false",
        "rtsp_reconnect_seconds":"5", "event_cooldown_seconds":"30"
    }
    for key,value in config_defaults.items(): con.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",(key,value))
    active_row=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()
    active_ok=active_row and con.execute("SELECT 1 FROM model_registry WHERE name=? AND status='ready'",(active_row[0],)).fetchone()
    if not active_ok:
        fallback=con.execute("SELECT name FROM model_registry WHERE status='ready' ORDER BY id DESC LIMIT 1").fetchone()
        value=fallback[0] if fallback else ""
        con.execute("INSERT INTO settings(key,value) VALUES('active_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(value,))
        if fallback: con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"WARNING","model_manager",f"Active model repaired to {fallback[0]}"))
    apply_retention(con)
    con.commit(); con.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try: yield
    finally:
        for task in list(_training_tasks.values()): task.cancel()
        if _training_tasks: await asyncio.gather(*list(_training_tasks.values()),return_exceptions=True)

app=FastAPI(title="ZMK Vision API",version="2.2.1",description="On-premise API контура видеоаналитики",lifespan=lifespan)
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
        if not WORKER_TOKEN: return JSONResponse({"detail":"Worker API is not configured"},status_code=503)
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
    try: too_large=bool(length and int(length)>2_000_000)
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

class CameraIn(BaseModel):
    name:str=Field(min_length=2,max_length=80)
    zone:str=Field(default="Без зоны",min_length=1,max_length=80)
    description:str=Field(default="",max_length=500)
    rtsp_url:str=Field(default="",max_length=2048)
    fps_limit:float=Field(default=8,ge=.1,le=60)
    enabled:bool=True
    @field_validator("rtsp_url")
    @classmethod
    def validate_rtsp(cls,value:str):
        if value and not value.startswith(("rtsp://","rtsps://")): raise ValueError("Требуется RTSP(S) URL")
        return value
class CameraUpdate(BaseModel):
    name:str=Field(min_length=2,max_length=80)
    zone:str=Field(default="Без зоны",min_length=1,max_length=80)
    description:str=Field(default="",max_length=500)
    rtsp_url:str|None=Field(default=None,max_length=2048)
    fps_limit:float=Field(default=8,ge=.1,le=60)
    enabled:bool=True
    @field_validator("rtsp_url")
    @classmethod
    def validate_rtsp(cls,value:str|None):
        if value and not value.startswith(("rtsp://","rtsps://")): raise ValueError("Требуется RTSP(S) URL")
        return value
class CameraTelemetry(BaseModel):
    status:Literal["online","offline","error","unknown"]
    fps:float=Field(default=0,ge=0,le=240)
    latency_ms:int=Field(default=0,ge=0,le=120000)
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

def rows(query,args=()):
    con=db(); result=[dict(r) for r in con.execute(query,args).fetchall()]; con.close(); return result

@app.get("/api/capabilities")
def capabilities():
    worker={"configured":bool(TRAINING_WORKER_URL),"reachable":False,"gpu":False}
    if TRAINING_WORKER_URL:
        try:
            response=httpx.get(f"{TRAINING_WORKER_URL}/health",timeout=2); response.raise_for_status(); data=response.json(); worker.update({"reachable":True,"gpu":bool(data.get("gpu"))})
        except httpx.HTTPError: pass
    return {"demo_mode":SEED_TEST_DATA,"training_worker":worker["reachable"] and worker["gpu"],"training":worker,"external_inference_gateway":True,"camera_crud":True,"diagnostics":True,"search":True}

@app.get("/api/health")
def health(): return {"status":"ok","version":"2.2.1","uptime_seconds":int(time.time()-STARTED),"time":now_iso()}

@app.get("/api/dashboard")
def dashboard():
    con=db(); total=con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]; online=con.execute("SELECT COUNT(*) FROM cameras WHERE status='online'").fetchone()[0]
    events24=con.execute("SELECT COUNT(*) FROM events WHERE timestamp >= ?",((datetime.now(TZ)-timedelta(days=1)).isoformat(),)).fetchone()[0]
    critical=con.execute("SELECT COUNT(*) FROM events WHERE severity='critical' AND acknowledged=0").fetchone()[0]
    avg=con.execute("SELECT COALESCE(AVG(fps),0), COALESCE(AVG(latency_ms),0) FROM cameras WHERE status='online'").fetchone()
    model=con.execute("SELECT m.name,m.precision,m.recall FROM model_registry m JOIN settings s ON s.key='active_model' AND s.value=m.name").fetchone()
    trend=[]
    for h in range(11,-1,-1):
        end=datetime.now(TZ)-timedelta(hours=h); start=end-timedelta(hours=1)
        n=con.execute("SELECT COUNT(*) FROM events WHERE timestamp BETWEEN ? AND ?",(start.isoformat(),end.isoformat())).fetchone()[0]
        trend.append({"label":end.strftime("%H:00"),"value":n})
    con.close(); gpu=gpu_metrics(); return {"cameras":{"total":total,"online":online},"events24h":events24,"critical_unacked":critical,"avg_fps":round(avg[0],1),"avg_latency_ms":round(avg[1]),"gpu_load":gpu["gpu"],"gpu_temp":gpu["gpu_temp"],"messenger_provider":MESSENGER_PROVIDER,"active_model":model[0] if model else None,"precision":model[1] if model else None,"recall":model[2] if model else None,"trend":trend}

@app.get("/api/internal/cameras")
def internal_cameras(): return rows("SELECT id,name,rtsp_url,fps_limit,enabled FROM cameras WHERE enabled=1 AND rtsp_url!='' ORDER BY id")

@app.get("/api/internal/active-model")
def internal_active_model():
    data=rows("SELECT m.name,m.format,m.artifact_uri,m.checksum FROM model_registry m JOIN settings s ON s.key='active_model' AND s.value=m.name WHERE m.status='ready'")
    return data[0] if data else None

@app.get("/api/cameras")
def cameras():
    return rows("SELECT id,name,zone,description,fps_limit,status,fps,latency_ms,enabled,created_at,updated_at,CASE WHEN rtsp_url='' THEN 0 ELSE 1 END AS configured FROM cameras ORDER BY created_at,id")

@app.get("/api/cameras/{camera_id}")
def camera_detail(camera_id:str):
    data=rows("SELECT id,name,zone,description,fps_limit,status,fps,latency_ms,enabled,created_at,updated_at,CASE WHEN rtsp_url='' THEN 0 ELSE 1 END AS configured FROM cameras WHERE id=?",(camera_id,))
    if not data: raise HTTPException(404,"Камера не найдена")
    return data[0]

@app.post("/api/cameras",status_code=201)
def add_camera(payload:CameraIn):
    cid=f"cam_{uuid.uuid4().hex[:12]}"; timestamp=now_iso(); con=db()
    con.execute("INSERT INTO cameras(id,name,zone,description,rtsp_url,fps_limit,status,fps,latency_ms,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(cid,payload.name,payload.zone,payload.description,payload.rtsp_url,payload.fps_limit,"unknown",0,0,int(payload.enabled),timestamp,timestamp)); con.commit(); con.close()
    return {"id":cid,"name":payload.name,"zone":payload.zone,"description":payload.description,"fps_limit":payload.fps_limit,"enabled":payload.enabled,"configured":bool(payload.rtsp_url),"status":"unknown"}

@app.put("/api/cameras/{camera_id}")
def update_camera(camera_id:str,payload:CameraUpdate):
    con=db(); current=con.execute("SELECT 1 FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not current: con.close(); raise HTTPException(404,"Камера не найдена")
    con.execute("UPDATE cameras SET name=?,zone=?,description=?,rtsp_url=COALESCE(?,rtsp_url),fps_limit=?,enabled=?,updated_at=? WHERE id=?",(payload.name,payload.zone,payload.description,payload.rtsp_url,payload.fps_limit,int(payload.enabled),now_iso(),camera_id)); con.commit(); con.close()
    return {"id":camera_id,"updated":True}

@app.delete("/api/cameras/{camera_id}")
def delete_camera(camera_id:str,delete_events:bool=False):
    con=db(); camera=con.execute("SELECT name FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not camera: con.close(); raise HTTPException(404,"Камера не найдена")
    event_count=con.execute("SELECT COUNT(*) FROM events WHERE camera_id=?",(camera_id,)).fetchone()[0]
    if event_count and not delete_events: con.close(); raise HTTPException(409,f"У камеры есть события: {event_count}. Подтвердите delete_events=true")
    con.execute("BEGIN IMMEDIATE")
    if delete_events: con.execute("DELETE FROM events WHERE camera_id=?",(camera_id,))
    con.execute("DELETE FROM cameras WHERE id=?",(camera_id,)); con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(now_iso(),"WARNING","camera_manager",f"Camera deleted: {camera[0]}",camera_id)); con.commit(); con.close()
    return {"id":camera_id,"deleted":True,"deleted_events":event_count if delete_events else 0}

@app.patch("/api/cameras/{camera_id}/toggle")
def toggle_camera(camera_id:str):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404,"Камера не найдена")
    enabled=0 if row[0] else 1; con.execute("UPDATE cameras SET enabled=?,updated_at=? WHERE id=?",(enabled,now_iso(),camera_id)); con.commit(); con.close(); return {"id":camera_id,"enabled":bool(enabled)}

@app.post("/api/cameras/{camera_id}/telemetry")
def camera_telemetry(camera_id:str,payload:CameraTelemetry):
    con=db(); cur=con.execute("UPDATE cameras SET status=?,fps=?,latency_ms=?,updated_at=? WHERE id=?",(payload.status,payload.fps,payload.latency_ms,now_iso(),camera_id)); con.commit(); con.close()
    if not cur.rowcount: raise HTTPException(404,"Камера не найдена")
    return {"id":camera_id,**payload.model_dump()}

def diagnose_camera_row(camera_id:str):
    con=db(); row=con.execute("SELECT id,name,rtsp_url,enabled FROM cameras WHERE id=?",(camera_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Камера не найдена")
    if not row[2]: return {"camera_id":camera_id,"name":row[1],"reachable":False,"status":"not_configured","latency_ms":None,"message":"RTSP URL не задан"}
    parsed=urlparse(row[2]); host=parsed.hostname; port=parsed.port or (322 if parsed.scheme=="rtsps" else 554)
    if not host: return {"camera_id":camera_id,"name":row[1],"reachable":False,"status":"invalid_url","latency_ms":None,"message":"Некорректный RTSP URL"}
    started=time.perf_counter()
    try:
        with socket.create_connection((host,port),timeout=3): pass
        latency=round((time.perf_counter()-started)*1000)
        return {"camera_id":camera_id,"name":row[1],"reachable":True,"status":"reachable","latency_ms":latency,"message":"TCP-подключение установлено"}
    except OSError as exc:
        return {"camera_id":camera_id,"name":row[1],"reachable":False,"status":"unreachable","latency_ms":None,"message":str(exc)[:200]}

@app.post("/api/cameras/{camera_id}/diagnostics")
def diagnose_camera(camera_id:str):
    return diagnose_camera_row(camera_id)

@app.get("/api/diagnostics")
def diagnostics():
    camera_ids=[r["id"] for r in rows("SELECT id FROM cameras ORDER BY id")]
    with ThreadPoolExecutor(max_workers=min(10,max(1,len(camera_ids)))) as pool: camera_results=list(pool.map(diagnose_camera_row,camera_ids))
    return {"generated_at":now_iso(),"system":system_health_data(),"cameras":camera_results}

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
        cam=con.execute("SELECT status,enabled FROM cameras WHERE id=?",(d.camera_id,)).fetchone()
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
            elif cam[0] != "online" or not cam[1]: reason="camera_unavailable"
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
@app.get("/api/models")
def models():
    active=rows("SELECT value FROM settings WHERE key='active_model'")[0]["value"]
    data=rows("SELECT name,format,status,precision,recall,trained_at,source,artifact_uri,checksum FROM model_registry ORDER BY id DESC")
    for item in data: item["active"]=item["name"]==active
    return data

@app.post("/api/models",status_code=201)
def register_model(payload:ModelIn):
    con=db()
    try: con.execute("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)",(payload.name,payload.format,"ready",payload.precision,payload.recall,now_iso(),payload.source,payload.artifact_uri,payload.checksum)); con.commit()
    except sqlite3.IntegrityError: con.close(); raise HTTPException(409,"Модель с таким именем уже существует")
    con.close(); return {"name":payload.name,"status":"ready","registered":True}
@app.get("/api/models/active/health")
def active_model_health():
    con=db(); active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone(); model=con.execute("SELECT name,format,status,precision,recall,trained_at,source FROM model_registry WHERE name=?",(active[0],)).fetchone() if active else None
    last=con.execute("SELECT timestamp,message FROM logs WHERE service='inference_gateway' ORDER BY id DESC LIMIT 1").fetchone(); limits={r[0]:float(r[1]) for r in con.execute("SELECT key,value FROM settings WHERE key IN ('min_model_precision','min_model_recall')").fetchall()}; con.close()
    if not model: raise HTTPException(503,"Активная модель отсутствует в реестре")
    healthy=model[2]=="ready" and model[3] is not None and model[4] is not None and model[3]>=limits.get('min_model_precision',90) and model[4]>=limits.get('min_model_recall',85)
    return {"healthy":healthy,"model":dict(model),"requirements":{"precision":limits.get('min_model_precision',90),"recall":limits.get('min_model_recall',85)},"last_inference":dict(last) if last else None}

@app.post("/api/models/{name}/activate")
def activate(name:str):
    started=time.perf_counter(); con=db(); model=con.execute("SELECT status,precision,recall FROM model_registry WHERE name=?",(name,)).fetchone()
    if not model: con.close(); raise HTTPException(404,"Модель не найдена")
    if model[0] != "ready": con.close(); raise HTTPException(409,"Модель ещё не готова")
    if model[1] is None or model[2] is None: con.close(); raise HTTPException(409,"У модели отсутствуют метрики валидации")
    limits={r[0]:float(r[1]) for r in con.execute("SELECT key,value FROM settings WHERE key IN ('min_model_precision','min_model_recall')").fetchall()}
    if model[1]<limits.get('min_model_precision',90) or model[2]<limits.get('min_model_recall',85): con.close(); raise HTTPException(409,"Метрики модели ниже минимально допустимых")
    con.execute("BEGIN IMMEDIATE"); old=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
    if old==name:
        con.commit(); con.close(); return {"active_model":name,"previous_model":old,"hot_swap":False,"idempotent":True,"control_plane_switch_ms":round((time.perf_counter()-started)*1000,2),"downtime_ms":0}
    con.execute("UPDATE settings SET value=? WHERE key='active_model'",(name,))
    con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","model_manager",f"Control-plane hot-swap {old} -> {name} completed"))
    con.commit(); con.close(); return {"active_model":name,"previous_model":old,"hot_swap":True,"idempotent":False,"control_plane_switch_ms":round((time.perf_counter()-started)*1000,2),"downtime_ms":0}

async def run_training(job_id:int):
    con=db(); con.execute("UPDATE training_jobs SET status='failed',stage='Worker не подключён',error='External GPU training worker is not configured',updated_at=? WHERE id=?",(now_iso(),job_id)); con.commit(); con.close()

@app.post("/api/training/jobs",status_code=202)
async def start_training(payload:TrainingIn):
    if not SEED_TEST_DATA and not TRAINING_WORKER_URL: raise HTTPException(503,"Сервис обучения не подключён. Запустите Compose profile training")
    con=db(); cam=con.execute("SELECT status,rtsp_url,fps_limit FROM cameras WHERE id=?",(payload.camera_id,)).fetchone()
    if not cam: con.close(); raise HTTPException(404,"Камера не найдена")
    if cam[0] != "online": con.close(); raise HTTPException(409,"Камера офлайн: кадры недоступны")
    active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
    target=payload.target_name or f"siz-auto-{payload.camera_id}-{datetime.now(TZ).strftime('%m%d-%H%M%S')}"
    if con.execute("SELECT 1 FROM model_registry WHERE name=?",(target,)).fetchone() or con.execute("SELECT 1 FROM training_jobs WHERE target_name=? AND status IN ('queued','running')",(target,)).fetchone():
        con.close(); raise HTTPException(409,"Имя модели уже используется")
    if con.execute("SELECT 1 FROM training_jobs WHERE status IN ('queued','running')").fetchone():
        con.close(); raise HTTPException(409,"Уже выполняется другая задача обучения")
    cur=con.execute("INSERT INTO training_jobs(created_at,updated_at,camera_id,base_model,target_name,image_count,epochs,status,progress,stage,batch,imgsz,patience,confidence,val_split,capture_fps) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(now_iso(),now_iso(),payload.camera_id,active,target,payload.image_count,payload.epochs,"queued",0,"В очереди",payload.batch,payload.imgsz,payload.patience,payload.confidence,payload.val_split,payload.capture_fps)); con.commit(); jid=cur.lastrowid; con.close()
    if SEED_TEST_DATA:
        task=asyncio.create_task(run_training(jid),name=f"training-{jid}"); _training_tasks[jid]=task; task.add_done_callback(lambda _: _training_tasks.pop(jid,None))
    else:
        model=rows("SELECT artifact_uri FROM model_registry WHERE name=?",(active,)); request={"id":jid,"camera_id":payload.camera_id,"rtsp_url":cam[1],"target_name":target,"base_artifact":model[0]["artifact_uri"] if model else None,"image_count":payload.image_count,"epochs":payload.epochs,"fps_limit":min(float(cam[2]),payload.capture_fps),"batch":payload.batch,"imgsz":payload.imgsz,"patience":payload.patience,"confidence":payload.confidence,"val_split":payload.val_split}
        try:
            async with httpx.AsyncClient(timeout=15) as client: response=await client.post(f"{TRAINING_WORKER_URL}/jobs",json=request); response.raise_for_status()
        except httpx.HTTPError as exc:
            con=db(); con.execute("UPDATE training_jobs SET status='failed',stage='Worker недоступен',error=?,updated_at=? WHERE id=?",(str(exc)[:500],now_iso(),jid)); con.commit(); con.close(); raise HTTPException(503,"Training worker недоступен")
    return {"id":jid,"status":"queued","target_name":target,"mode":"pseudo-label fine-tuning"}

@app.put("/api/training/jobs/{job_id}/progress")
def training_progress(job_id:int,payload:TrainingProgress):
    con=db(); job=con.execute("SELECT target_name,camera_id FROM training_jobs WHERE id=?",(job_id,)).fetchone()
    if not job: con.close(); raise HTTPException(404,"Задача не найдена")
    con.execute("UPDATE training_jobs SET status=?,progress=?,stage=?,error=?,updated_at=? WHERE id=?",(payload.status,payload.progress,payload.stage,payload.error,now_iso(),job_id))
    if payload.status=="completed" and payload.artifact_uri and payload.precision is not None and payload.recall is not None:
        con.execute("INSERT OR REPLACE INTO model_registry(name,format,status,precision,recall,trained_at,source,artifact_uri,checksum) VALUES(?,?,?,?,?,?,?,?,?)",(job[0],"ONNX","ready",payload.precision,payload.recall,now_iso(),f"camera:{job[1]}",payload.artifact_uri,""))
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
    gpu=gpu_metrics(); con=db(); con.execute("SELECT 1").fetchone(); camera_count=con.execute("SELECT COUNT(*) FROM cameras WHERE enabled=1").fetchone()[0]; model=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone(); con.close()
    return {"cpu":round(psutil.cpu_percent(interval=.05),1),"ram":round(psutil.virtual_memory().percent,1),"disk":round(psutil.disk_usage(str(DB_PATH.parent)).percent,1),**gpu,"messenger_provider":MESSENGER_PROVIDER,"services":[{"name":"api","status":"healthy"},{"name":"database","status":"healthy"},{"name":"ingestion","status":"configured" if camera_count else "not_configured"},{"name":"inference","status":"configured" if model and model[0] else "not_configured"}]}

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
