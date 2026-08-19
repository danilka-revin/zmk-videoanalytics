from __future__ import annotations
import asyncio, csv, hashlib, hmac, io, json, os, random, sqlite3, time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

TZ = timezone(timedelta(hours=7))
DB_PATH = Path(os.getenv("VIDEOANALYTICS_DB", str(Path(__file__).resolve().parent.parent / "videoanalytics.db")))
STARTED = time.time()
API_KEY = os.getenv("ZMK_API_KEY", "").strip()
try: RATE_LIMIT_PER_MINUTE = max(10,int(os.getenv("RATE_LIMIT_PER_MINUTE", "120")))
except ValueError: RATE_LIMIT_PER_MINUTE = 120
_rate_buckets: dict[str, list[float]] = {}
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ROLES = {**{int(x):"viewer" for x in os.getenv("TELEGRAM_VIEWER_IDS","").split(",") if x.strip().isdigit()},**{int(x):"operator" for x in os.getenv("TELEGRAM_OPERATOR_IDS","").split(",") if x.strip().isdigit()},**{int(x):"admin" for x in os.getenv("TELEGRAM_ADMIN_IDS","").split(",") if x.strip().isdigit()}}
EVENT_TYPES = ["no_helmet", "no_vest", "phone_usage", "smoking", "restricted_zone", "immobility"]
SEVERITIES = ["critical", "high", "medium", "low"]

def now_iso(): return datetime.now(TZ).isoformat(timespec="seconds")
def db():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    con = sqlite3.connect(DB_PATH,timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY, name TEXT NOT NULL, zone TEXT NOT NULL, rtsp_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, fps REAL NOT NULL, latency_ms INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, camera_id TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, confidence REAL NOT NULL, person_id TEXT, acknowledged INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', FOREIGN KEY(camera_id) REFERENCES cameras(id));
    CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, level TEXT NOT NULL, service TEXT NOT NULL, message TEXT NOT NULL, camera_id TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS model_registry(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, format TEXT NOT NULL, status TEXT NOT NULL, precision REAL, recall REAL, trained_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'baseline');
    CREATE TABLE IF NOT EXISTS training_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, camera_id TEXT NOT NULL, base_model TEXT NOT NULL, target_name TEXT NOT NULL, image_count INTEGER NOT NULL, epochs INTEGER NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL, error TEXT, FOREIGN KEY(camera_id) REFERENCES cameras(id));
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, login TEXT UNIQUE NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
    """)
    con.execute("UPDATE training_jobs SET status='failed',stage='Прервано перезапуском',error='Worker restarted before completion',updated_at=? WHERE status IN ('queued','running')",(now_iso(),))
    if con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 0:
        cams=[(f"cam_{i:02}", f"Камера {i:02}", ["Цех №1","Склад","Проходная","Зона погрузки"][i%4], os.getenv(f"RTSP_CAM_{i:02}",f"rtsp://camera-{i:02}/stream"), "online" if i not in (7,) else "offline", round(random.uniform(6.8,9.8),1), random.randint(110,420),1,now_iso()) for i in range(1,11)]
        con.executemany("INSERT INTO cameras VALUES(?,?,?,?,?,?,?,?,?)",cams)
        for i in range(48):
            ts=(datetime.now(TZ)-timedelta(minutes=i*37)).isoformat(timespec="seconds")
            typ=EVENT_TYPES[i%len(EVENT_TYPES)]; sev=SEVERITIES[i%len(SEVERITIES)]
            con.execute("INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,acknowledged,note) VALUES(?,?,?,?,?,?,?,?)",(ts,f"cam_{i%10+1:02}",typ,sev,round(.72+(i%25)/100,2),f"P-{1000+i}",1 if i%5==0 else 0,""))
        for level,msg in [("INFO","Сервис аналитики запущен"),("WARNING","Снижение FPS на cam_07"),("INFO","Модель siz-guard-v2.1 загружена")]:
            con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(now_iso(),level,"ai_inference",msg,"cam_07" if "07" in msg else None))
        defaults={"helmet_conf":"0.85","vest_conf":"0.80","phone_conf":"0.78","active_model":"siz-guard-v2.1"}
        con.executemany("INSERT INTO settings VALUES(?,?)", defaults.items())
    config_defaults={
        "site_name":"ZMK Vision", "timezone":"Asia/Krasnoyarsk", "language":"ru",
        "retention_days":"90", "archive_quality":"90", "archive_clip_seconds":"10",
        "inference_fps":"8", "inference_device":"cuda:0", "batch_size":"4", "nms_iou":"0.45",
        "telegram_enabled":"false", "telegram_chat_ids":"", "critical_alerts":"true",
        "webhook_enabled":"false", "webhook_url":"", "webhook_timeout":"5",
        "minio_endpoint":"minio:9000", "minio_bucket":"videoanalytics", "minio_secure":"false",
        "rtsp_reconnect_seconds":"5", "event_cooldown_seconds":"30", "auto_training_enabled":"false"
    }
    for key,value in config_defaults.items(): con.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",(key,value))
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        con.executemany("INSERT INTO users(name,login,role,active,created_at) VALUES(?,?,?,?,?)",[("Алексей Петров","admin","admin",1,now_iso()),("Оператор смены","operator","operator",1,now_iso()),("Наблюдатель","viewer","viewer",1,now_iso())])
    if con.execute("SELECT COUNT(*) FROM model_registry").fetchone()[0] == 0:
        con.executemany("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source) VALUES(?,?,?,?,?,?,?)", [
            ("siz-guard-v2.1","TensorRT FP16","ready",92.4,87.1,now_iso(),"baseline"),
            ("siz-guard-v2.0","ONNX FP32","ready",90.8,85.9,now_iso(),"baseline")])
    con.commit(); con.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); yield

app=FastAPI(title="ZMK Vision API",version="1.2.2",description="On-premise API контура видеоаналитики",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[x.strip() for x in os.getenv("CORS_ORIGINS","http://localhost:5173").split(",") if x.strip()],allow_credentials=True,allow_methods=["GET","POST","PUT","PATCH","DELETE"],allow_headers=["Content-Type","X-API-Key","X-Telegram-Init-Data"])

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
    api_key_ok=bool(API_KEY and hmac.compare_digest(request.headers.get("X-API-Key",""),API_KEY))
    telegram_role=telegram_webapp_role(request.headers.get("X-Telegram-Init-Data",""))
    if API_KEY and not public and not (api_key_ok or telegram_role):
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
        now=time.time(); key=f"{request.client.host if request.client else 'unknown'}:{path.split('/')[2]}"
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
    name:str=Field(min_length=2,max_length=80); zone:str=Field(min_length=2,max_length=80); rtsp_url:str=Field(default="",max_length=2048); enabled:bool=True
    @field_validator("rtsp_url")
    @classmethod
    def validate_rtsp(cls,value:str):
        if value and not value.startswith(("rtsp://","rtsps://")): raise ValueError("Требуется RTSP(S) URL")
        return value
class SettingIn(BaseModel): value:float=Field(ge=.1,le=1)
class AckIn(BaseModel): note:str=Field(default="",max_length=500)
class DetectionIn(BaseModel):
    camera_id:str=Field(min_length=1,max_length=64)
    model_name:str=Field(min_length=1,max_length=120)
    timestamp:datetime|None=None
    event_type:Literal["no_helmet","no_vest","phone_usage","smoking","restricted_zone","immobility"]
    confidence:float=Field(ge=0,le=1)
    person_id:str|None=Field(default=None,max_length=120)
    bbox:list[float]=Field(default_factory=list,min_length=0,max_length=4)
class DetectionBatch(BaseModel): detections:list[DetectionIn]=Field(min_length=1,max_length=500)
class ConfigPatch(BaseModel): values:dict[str,Any]
class UserIn(BaseModel):
    name:str=Field(min_length=2,max_length=80)
    login:str=Field(min_length=2,max_length=40,pattern=r"^[a-zA-Z0-9._-]+$")
    role:Literal["admin","operator","viewer"]
class TrainingIn(BaseModel):
    camera_id:str
    image_count:int=Field(default=100,ge=20,le=5000)
    epochs:int=Field(default=20,ge=1,le=300)
    target_name:str|None=Field(default=None,min_length=2,max_length=120,pattern=r"^[a-zA-Z0-9._-]+$")

def rows(query,args=()):
    con=db(); result=[dict(r) for r in con.execute(query,args).fetchall()]; con.close(); return result

@app.get("/api/health")
def health(): return {"status":"ok","version":"1.2.2","uptime_seconds":int(time.time()-STARTED),"time":now_iso()}

@app.get("/api/dashboard")
def dashboard():
    con=db(); total=con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]; online=con.execute("SELECT COUNT(*) FROM cameras WHERE status='online'").fetchone()[0]
    events24=con.execute("SELECT COUNT(*) FROM events WHERE timestamp >= ?",((datetime.now(TZ)-timedelta(days=1)).isoformat(),)).fetchone()[0]
    critical=con.execute("SELECT COUNT(*) FROM events WHERE severity='critical' AND acknowledged=0").fetchone()[0]
    avg=con.execute("SELECT COALESCE(AVG(fps),0), COALESCE(AVG(latency_ms),0) FROM cameras WHERE status='online'").fetchone()
    trend=[]
    for h in range(11,-1,-1):
        end=datetime.now(TZ)-timedelta(hours=h); start=end-timedelta(hours=1)
        n=con.execute("SELECT COUNT(*) FROM events WHERE timestamp BETWEEN ? AND ?",(start.isoformat(),end.isoformat())).fetchone()[0]
        trend.append({"label":end.strftime("%H:00"),"value":n})
    con.close(); return {"cameras":{"total":total,"online":online},"events24h":events24,"critical_unacked":critical,"avg_fps":round(avg[0],1),"avg_latency_ms":round(avg[1]),"gpu_load":68,"precision":92.4,"recall":87.1,"trend":trend}

@app.get("/api/cameras")
def cameras():
    # RTSP credentials never leave the backend through list endpoints.
    return rows("SELECT id,name,zone,status,fps,latency_ms,enabled,updated_at,CASE WHEN rtsp_url='' THEN 0 ELSE 1 END AS configured FROM cameras ORDER BY id")
@app.post("/api/cameras",status_code=201)
def add_camera(payload:CameraIn):
    con=db(); num=con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]+1; cid=f"cam_{num:02}"
    con.execute("INSERT INTO cameras VALUES(?,?,?,?,?,?,?,?,?)",(cid,payload.name,payload.zone,payload.rtsp_url,"offline",0,0,int(payload.enabled),now_iso())); con.commit(); con.close(); return {"id":cid,"name":payload.name,"zone":payload.zone,"enabled":payload.enabled,"configured":bool(payload.rtsp_url)}
@app.patch("/api/cameras/{camera_id}/toggle")
def toggle_camera(camera_id:str):
    con=db(); row=con.execute("SELECT enabled FROM cameras WHERE id=?",(camera_id,)).fetchone()
    if not row: raise HTTPException(404,"Камера не найдена")
    enabled=0 if row[0] else 1; con.execute("UPDATE cameras SET enabled=?,updated_at=? WHERE id=?",(enabled,now_iso(),camera_id)); con.commit(); con.close(); return {"id":camera_id,"enabled":bool(enabled)}

@app.get("/api/events")
def events(limit:int=Query(50,ge=1,le=500),severity:str|None=None,event_type:str|None=None,acknowledged:bool|None=None):
    where=[]; args=[]
    if severity: where.append("severity=?"); args.append(severity)
    if event_type: where.append("type=?"); args.append(event_type)
    if acknowledged is not None: where.append("acknowledged=?"); args.append(int(acknowledged))
    q="SELECT e.*,c.name camera_name,c.zone FROM events e JOIN cameras c ON c.id=e.camera_id"+(" WHERE "+" AND ".join(where) if where else "")+" ORDER BY timestamp DESC LIMIT ?"; args.append(limit)
    return rows(q,args)
@app.post("/api/events/{event_id}/ack")
def ack(event_id:int,payload:AckIn):
    con=db(); cur=con.execute("UPDATE events SET acknowledged=1,note=? WHERE id=?",(payload.note,event_id)); con.commit(); con.close()
    if not cur.rowcount: raise HTTPException(404,"Событие не найдено")
    return {"id":event_id,"acknowledged":True}
@app.post("/api/events/simulate",status_code=201)
def simulate_event():
    typ=random.choice(EVENT_TYPES); sev=random.choice(SEVERITIES[:3]); cam=f"cam_{random.randint(1,10):02}"; conf=round(random.uniform(.78,.98),2)
    con=db(); cur=con.execute("INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id) VALUES(?,?,?,?,?,?)",(now_iso(),cam,typ,sev,conf,f"P-{random.randint(1100,9999)}")); con.commit(); eid=cur.lastrowid; con.close()
    return {"id":eid,"timestamp":now_iso(),"camera_id":cam,"type":typ,"severity":sev,"confidence":conf}

@app.post("/api/inference/detections")
def ingest_detections(payload:DetectionBatch):
    """Validated contract from inference workers to the event subsystem."""
    con=db(); active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
    thresholds={"no_helmet":"helmet_conf","no_vest":"vest_conf","phone_usage":"phone_conf"}
    accepted=[]; rejected=[]
    for i,d in enumerate(payload.detections):
        cam=con.execute("SELECT status,enabled FROM cameras WHERE id=?",(d.camera_id,)).fetchone()
        reason=None
        if d.model_name != active: reason=f"stale_model: active={active}"
        elif not cam: reason="unknown_camera"
        elif cam[0] != "online" or not cam[1]: reason="camera_unavailable"
        else:
            key=thresholds.get(d.event_type); threshold=float(con.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()[0]) if key else .70
            if d.confidence < threshold: reason=f"below_threshold:{threshold}"
        if reason: rejected.append({"index":i,"reason":reason}); continue
        severity="critical" if d.event_type in {"restricted_zone","immobility"} else "high" if d.event_type in {"no_helmet","smoking"} else "medium"
        cur=con.execute("INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id) VALUES(?,?,?,?,?,?)",(d.timestamp.isoformat() if d.timestamp else now_iso(),d.camera_id,d.event_type,severity,d.confidence,d.person_id))
        accepted.append({"index":i,"event_id":cur.lastrowid})
    con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","inference_gateway",f"batch model={active} accepted={len(accepted)} rejected={len(rejected)}"))
    con.commit(); con.close(); return {"active_model":active,"accepted":accepted,"rejected":rejected,"received":len(payload.detections)}

@app.get("/api/admin/config")
def get_config():
    data={r["key"]:r["value"] for r in rows("SELECT * FROM settings")}
    groups={
      "general":["site_name","timezone","language","retention_days"],
      "inference":["inference_fps","inference_device","batch_size","nms_iou","helmet_conf","vest_conf","phone_conf","event_cooldown_seconds"],
      "archive":["archive_quality","archive_clip_seconds","minio_endpoint","minio_bucket","minio_secure"],
      "notifications":["telegram_enabled","telegram_chat_ids","critical_alerts"],
      "integration":["webhook_enabled","webhook_url","webhook_timeout","rtsp_reconnect_seconds"],
      "training":["auto_training_enabled"]}
    return {g:{k:data.get(k,"") for k in keys} for g,keys in groups.items()}

CONFIG_ALLOWED={"site_name","timezone","language","retention_days","inference_fps","inference_device","batch_size","nms_iou","helmet_conf","vest_conf","phone_conf","event_cooldown_seconds","archive_quality","archive_clip_seconds","minio_endpoint","minio_bucket","minio_secure","telegram_enabled","telegram_chat_ids","critical_alerts","webhook_enabled","webhook_url","webhook_timeout","rtsp_reconnect_seconds","auto_training_enabled"}
@app.put("/api/admin/config")
def update_config(payload:ConfigPatch):
    unknown=set(payload.values)-CONFIG_ALLOWED
    if unknown: raise HTTPException(422,f"Неизвестные параметры: {', '.join(sorted(unknown))}")
    numeric={"retention_days":(1,3650),"inference_fps":(1,30),"batch_size":(1,64),"nms_iou":(.1,.95),"helmet_conf":(.1,1),"vest_conf":(.1,1),"phone_conf":(.1,1),"event_cooldown_seconds":(0,3600),"archive_quality":(10,100),"archive_clip_seconds":(2,120),"webhook_timeout":(1,60),"rtsp_reconnect_seconds":(1,300)}
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
    con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","admin",f"Configuration updated: {', '.join(payload.values.keys())}")); con.commit(); con.close()
    return {"updated":list(payload.values),"restart_required":any(k in payload.values for k in {"inference_device","minio_endpoint"})}

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
def logs(level:str|None=None,limit:int=Query(100,le=500)):
    return rows("SELECT * FROM logs"+(" WHERE level=?" if level else "")+" ORDER BY id DESC LIMIT ?",([level,limit] if level else [limit]))
@app.get("/api/settings")
def settings(): return {r["key"]:r["value"] for r in rows("SELECT * FROM settings")}
@app.put("/api/settings/{key}")
def update_setting(key:Literal["helmet_conf","vest_conf","phone_conf"],payload:SettingIn):
    con=db(); con.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(payload.value))); con.commit(); con.close(); return {"key":key,"value":payload.value}
@app.get("/api/models")
def models():
    active=rows("SELECT value FROM settings WHERE key='active_model'")[0]["value"]
    data=rows("SELECT name,format,status,precision,recall,trained_at,source FROM model_registry ORDER BY id DESC")
    for item in data: item["active"]=item["name"]==active
    return data

@app.post("/api/models/{name}/activate")
def activate(name:str):
    con=db(); model=con.execute("SELECT status FROM model_registry WHERE name=?",(name,)).fetchone()
    if not model: con.close(); raise HTTPException(404,"Модель не найдена")
    if model[0] != "ready": con.close(); raise HTTPException(409,"Модель ещё не готова")
    con.execute("BEGIN IMMEDIATE"); old=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
    con.execute("UPDATE settings SET value=? WHERE key='active_model'",(name,))
    con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"INFO","model_manager",f"Hot-swap {old} -> {name} completed"))
    con.commit(); con.close(); return {"active_model":name,"previous_model":old,"hot_swap":True,"downtime_ms":0}

async def run_training(job_id:int):
    try:
        stages=[(8,"Захват кадров с RTSP"),(22,"Контроль качества изображений"),(38,"Псевдоразметка базовой моделью"),(55,"Подготовка train/val выборки"),(72,"Дообучение YOLO"),(88,"Валидация метрик"),(96,"Экспорт ONNX"),(100,"Модель готова")]
        for progress,stage in stages:
            await asyncio.sleep(.7)
            con=db(); con.execute("UPDATE training_jobs SET status=?,progress=?,stage=?,updated_at=? WHERE id=?",("running" if progress<100 else "completed",progress,stage,now_iso(),job_id)); con.commit(); con.close()
        con=db(); job=con.execute("SELECT target_name,camera_id FROM training_jobs WHERE id=?",(job_id,)).fetchone()
        if not job: con.close(); return
        con.execute("INSERT INTO model_registry(name,format,status,precision,recall,trained_at,source) VALUES(?,?,?,?,?,?,?)",(job[0],"ONNX FP16","ready",round(random.uniform(91.5,94.2),1),round(random.uniform(86.0,89.4),1),now_iso(),f"camera:{job[1]}"))
        con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(now_iso(),"INFO","training",f"Training completed: {job[0]}",job[1])); con.commit(); con.close()
    except Exception as exc:
        con=db(); con.execute("UPDATE training_jobs SET status='failed',stage='Ошибка',error=?,updated_at=? WHERE id=?",(str(exc)[:500],now_iso(),job_id)); con.execute("INSERT INTO logs(timestamp,level,service,message) VALUES(?,?,?,?)",(now_iso(),"ERROR","training",f"Job {job_id} failed: {str(exc)[:300]}")); con.commit(); con.close()

@app.post("/api/training/jobs",status_code=202)
async def start_training(payload:TrainingIn):
    con=db(); cam=con.execute("SELECT status FROM cameras WHERE id=?",(payload.camera_id,)).fetchone()
    if not cam: con.close(); raise HTTPException(404,"Камера не найдена")
    if cam[0] != "online": con.close(); raise HTTPException(409,"Камера офлайн: кадры недоступны")
    active=con.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()[0]
    target=payload.target_name or f"siz-auto-{payload.camera_id}-{datetime.now(TZ).strftime('%m%d-%H%M%S')}"
    if con.execute("SELECT 1 FROM model_registry WHERE name=?",(target,)).fetchone() or con.execute("SELECT 1 FROM training_jobs WHERE target_name=? AND status IN ('queued','running')",(target,)).fetchone():
        con.close(); raise HTTPException(409,"Имя модели уже используется")
    cur=con.execute("INSERT INTO training_jobs(created_at,updated_at,camera_id,base_model,target_name,image_count,epochs,status,progress,stage) VALUES(?,?,?,?,?,?,?,?,?,?)",(now_iso(),now_iso(),payload.camera_id,active,target,payload.image_count,payload.epochs,"queued",0,"В очереди")); con.commit(); jid=cur.lastrowid; con.close()
    asyncio.create_task(run_training(jid))
    return {"id":jid,"status":"queued","target_name":target,"mode":"pseudo-label fine-tuning"}

@app.get("/api/training/jobs")
def training_jobs(): return rows("SELECT * FROM training_jobs ORDER BY id DESC LIMIT 30")
@app.get("/api/training/jobs/{job_id}")
def training_job(job_id:int):
    data=rows("SELECT * FROM training_jobs WHERE id=?",(job_id,))
    if not data: raise HTTPException(404,"Задача не найдена")
    return data[0]

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
@app.post("/api/admin/logs/simulate-error",status_code=201)
def simulate_error():
    samples=[("ERROR","ai_inference","CUDA out of memory during batch inference"),("CRITICAL","ingestion","RTSP stream unavailable for 30 seconds"),("WARNING","archive","MinIO write latency exceeded 2 seconds")]
    level,service,message=random.choice(samples); cam=f"cam_{random.randint(1,10):02}"
    con=db(); cur=con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(now_iso(),level,service,message,cam)); con.commit(); lid=cur.lastrowid; con.close(); return {"id":lid,"level":level,"service":service,"message":message,"camera_id":cam,"timestamp":now_iso()}

@app.get("/api/system-health")
def system_health(): return {"cpu":41,"ram":57,"gpu":68,"vram":72,"disk":38,"services":[{"name":n,"status":"healthy"} for n in ["ingestion","inference","events","archive","api"]]}
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
            yield f"data: {json.dumps({'time':now_iso(),'gpu':random.randint(63,73)},ensure_ascii=False)}\n\n"; await asyncio.sleep(3)
    return StreamingResponse(generate(),media_type="text/event-stream")
