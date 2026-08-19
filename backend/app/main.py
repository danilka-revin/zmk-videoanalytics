from __future__ import annotations
import asyncio, csv, io, json, random, sqlite3, time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

TZ = timezone(timedelta(hours=7))
DB_PATH = Path(__file__).resolve().parent.parent / "videoanalytics.db"
STARTED = time.time()
EVENT_TYPES = ["no_helmet", "no_vest", "phone_usage", "smoking", "restricted_zone", "immobility"]
SEVERITIES = ["critical", "high", "medium", "low"]

def now_iso(): return datetime.now(TZ).isoformat(timespec="seconds")
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY, name TEXT NOT NULL, zone TEXT NOT NULL, rtsp_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, fps REAL NOT NULL, latency_ms INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, camera_id TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, confidence REAL NOT NULL, person_id TEXT, acknowledged INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', FOREIGN KEY(camera_id) REFERENCES cameras(id));
    CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, level TEXT NOT NULL, service TEXT NOT NULL, message TEXT NOT NULL, camera_id TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    if con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 0:
        cams=[(f"cam_{i:02}", f"Камера {i:02}", ["Цех №1","Склад","Проходная","Зона погрузки"][i%4], f"rtsp://camera-{i:02}/stream", "online" if i not in (7,) else "offline", round(random.uniform(6.8,9.8),1), random.randint(110,420),1,now_iso()) for i in range(1,11)]
        con.executemany("INSERT INTO cameras VALUES(?,?,?,?,?,?,?,?,?)",cams)
        for i in range(48):
            ts=(datetime.now(TZ)-timedelta(minutes=i*37)).isoformat(timespec="seconds")
            typ=EVENT_TYPES[i%len(EVENT_TYPES)]; sev=SEVERITIES[i%len(SEVERITIES)]
            con.execute("INSERT INTO events(timestamp,camera_id,type,severity,confidence,person_id,acknowledged,note) VALUES(?,?,?,?,?,?,?,?)",(ts,f"cam_{i%10+1:02}",typ,sev,round(.72+(i%25)/100,2),f"P-{1000+i}",1 if i%5==0 else 0,""))
        for level,msg in [("INFO","Сервис аналитики запущен"),("WARNING","Снижение FPS на cam_07"),("INFO","Модель siz-guard-v2.1 загружена")]:
            con.execute("INSERT INTO logs(timestamp,level,service,message,camera_id) VALUES(?,?,?,?,?)",(now_iso(),level,"ai_inference",msg,"cam_07" if "07" in msg else None))
        defaults={"helmet_conf":"0.85","vest_conf":"0.80","phone_conf":"0.78","active_model":"siz-guard-v2.1"}
        con.executemany("INSERT INTO settings VALUES(?,?)", defaults.items())
    con.commit(); con.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); yield

app=FastAPI(title="ZMK Vision API",version="1.0.0",description="On-premise API контура видеоаналитики",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["http://localhost:5173"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])

class CameraIn(BaseModel):
    name:str=Field(min_length=2,max_length=80); zone:str=Field(min_length=2,max_length=80); rtsp_url:str=""; enabled:bool=True
class SettingIn(BaseModel): value:float=Field(ge=.1,le=1)
class AckIn(BaseModel): note:str=Field(default="",max_length=500)

def rows(query,args=()):
    con=db(); result=[dict(r) for r in con.execute(query,args).fetchall()]; con.close(); return result

@app.get("/api/health")
def health(): return {"status":"ok","version":"1.0.0","uptime_seconds":int(time.time()-STARTED),"time":now_iso()}

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
def cameras(): return rows("SELECT * FROM cameras ORDER BY id")
@app.post("/api/cameras",status_code=201)
def add_camera(payload:CameraIn):
    con=db(); num=con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]+1; cid=f"cam_{num:02}"
    con.execute("INSERT INTO cameras VALUES(?,?,?,?,?,?,?,?,?)",(cid,payload.name,payload.zone,payload.rtsp_url,"offline",0,0,int(payload.enabled),now_iso())); con.commit(); con.close(); return {"id":cid,**payload.model_dump()}
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
    return [{"name":"siz-guard-v2.1","format":"TensorRT","precision":92.4,"recall":87.1,"active":active=="siz-guard-v2.1"},{"name":"siz-guard-v2.0","format":"ONNX","precision":90.8,"recall":85.9,"active":active=="siz-guard-v2.0"}]
@app.post("/api/models/{name}/activate")
def activate(name:str):
    if name not in {"siz-guard-v2.1","siz-guard-v2.0"}: raise HTTPException(404,"Модель не найдена")
    con=db(); con.execute("UPDATE settings SET value=? WHERE key='active_model'",(name,)); con.commit(); con.close(); return {"active_model":name,"hot_swap":True}
@app.get("/api/system-health")
def system_health(): return {"cpu":41,"ram":57,"gpu":68,"vram":72,"disk":38,"services":[{"name":n,"status":"healthy"} for n in ["ingestion","inference","events","archive","api"]]}
@app.get("/api/reports/events.csv")
def report_csv():
    data=rows("SELECT timestamp,camera_id,type,severity,confidence,person_id,acknowledged,note FROM events ORDER BY timestamp DESC")
    out=io.StringIO(); w=csv.DictWriter(out,fieldnames=data[0].keys() if data else []); w.writeheader(); w.writerows(data)
    return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=zmk-events.csv"})
@app.get("/api/stream")
async def stream():
    async def generate():
        while True:
            yield f"data: {json.dumps({'time':now_iso(),'gpu':random.randint(63,73)},ensure_ascii=False)}\n\n"; await asyncio.sleep(3)
    return StreamingResponse(generate(),media_type="text/event-stream")
