from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import httpx
import torch
from ultralytics import YOLO

API=os.getenv('ZMK_API_URL','http://api:8000').rstrip('/'); API_KEY=os.getenv('ZMK_API_KEY',''); WORKER_TOKEN=os.getenv('ZMK_WORKER_TOKEN',''); DEVICE=os.getenv('INFERENCE_DEVICE','0' if torch.cuda.is_available() else 'cpu'); CONF=float(os.getenv('INFERENCE_CONF','0.5'))
EVENT_CLASSES={'no_helmet','no_vest','phone_usage','smoking','restricted_zone','immobility'}
def file_sha256(path:Path):
 hasher=hashlib.sha256()
 with path.open('rb') as stream:
  for chunk in iter(lambda:stream.read(1024*1024),b''): hasher.update(chunk)
 return hasher.hexdigest()
class Runtime:
 def __init__(self): self.model=None; self.model_name=''; self.captures={}; self.last_telemetry={}; self.frame_counts={}
 async def get(self,path,internal=False):
  headers={'X-Worker-Token':WORKER_TOKEN} if internal else ({'X-API-Key':API_KEY} if API_KEY else {})
  async with httpx.AsyncClient(headers=headers,timeout=15) as c: r=await c.get(API+path); r.raise_for_status(); return r.json()
 async def post(self,path,data):
  headers={'X-API-Key':API_KEY} if API_KEY else {}
  async with httpx.AsyncClient(headers=headers,timeout=15) as c: r=await c.post(API+path,json=data); r.raise_for_status(); return r.json()
 async def load_model(self):
  info=await self.get('/api/internal/active-model',True)
  if not info: self.model=None; self.model_name=''; return
  if info['name']==self.model_name: return
  artifact=info['artifact_uri'].removeprefix('file://'); path=Path(artifact)
  if not path.exists(): raise RuntimeError(f'Model artifact not found: {artifact}')
  if info.get('checksum'):
   digest=await asyncio.to_thread(file_sha256,path)
   if digest.lower()!=info['checksum'].lower(): raise RuntimeError('Model checksum mismatch')
  self.model=await asyncio.to_thread(YOLO,str(path)); self.model_name=info['name']
 async def frame(self,cam):
  cid=cam['id']; cap=self.captures.get(cid)
  if cap is None or not cap.isOpened():
   cap=cv2.VideoCapture(); cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,5000); cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC,5000); cap.open(cam['rtsp_url']); self.captures[cid]=cap
  started=time.perf_counter(); ok,image=await asyncio.to_thread(cap.read); latency=round((time.perf_counter()-started)*1000)
  now=time.time(); self.frame_counts[cid]=self.frame_counts.get(cid,0)+(1 if ok else 0)
  if now-self.last_telemetry.get(cid,now)>10 or cid not in self.last_telemetry:
   elapsed=max(.001,now-self.last_telemetry.get(cid,now)); effective=self.frame_counts[cid]/elapsed if cid in self.last_telemetry else 0
   await self.post(f'/api/cameras/{cid}/telemetry',{'status':'online' if ok else 'offline','fps':effective,'latency_ms':latency}); self.last_telemetry[cid]=now; self.frame_counts[cid]=0
  if not ok or self.model is None: return
  result=(await asyncio.to_thread(self.model.track,image,persist=True,conf=CONF,device=DEVICE,verbose=False))[0]; detections=[]; stamp=datetime.now(timezone.utc).isoformat()
  track_ids=result.boxes.id.int().cpu().tolist() if result.boxes.id is not None else [None]*len(result.boxes)
  for index,(xyxy,cls,score,track_id) in enumerate(zip(result.boxes.xyxy.cpu().tolist(),result.boxes.cls.cpu().tolist(),result.boxes.conf.cpu().tolist(),track_ids)):
   label=str(self.model.names[int(cls)])
   if label not in EVENT_CLASSES: continue
   x1,y1,x2,y2=xyxy; detections.append({'camera_id':cid,'model_name':self.model_name,'timestamp':stamp,'event_type':label,'confidence':score,'person_id':f'{cid}-track-{track_id}' if track_id is not None else None,'detection_id':f'{cid}:{int(time.time()*1000)}:{index}','bbox':[x1,y1,x2,y2]})
  if detections: await self.post('/api/inference/detections',{'detections':detections})
 async def run(self):
  if not WORKER_TOKEN: raise RuntimeError('ZMK_WORKER_TOKEN is required')
  while True:
   try:
    await self.load_model(); cameras=await self.get('/api/internal/cameras',True); active_ids={c['id'] for c in cameras}
    for stale in set(self.captures)-active_ids: self.captures.pop(stale).release(); self.last_telemetry.pop(stale,None); self.frame_counts.pop(stale,None)
    for cam in cameras:
     await self.frame(cam); await asyncio.sleep(max(.01,1/float(cam['fps_limit'])))
    if not cameras: await asyncio.sleep(5)
   except Exception as exc:  # noqa: BLE001 - keep long-running worker alive
    print(f'inference loop error: {exc}',flush=True); await asyncio.sleep(3)
if __name__=='__main__': asyncio.run(Runtime().run())
