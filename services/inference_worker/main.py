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
class Runtime:
 def __init__(self): self.model=None; self.model_name=''; self.captures={}; self.last_telemetry={}
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
   digest=hashlib.sha256(path.read_bytes()).hexdigest()
   if digest.lower()!=info['checksum'].lower(): raise RuntimeError('Model checksum mismatch')
  self.model=await asyncio.to_thread(YOLO,str(path)); self.model_name=info['name']
 async def frame(self,cam):
  cid=cam['id']; cap=self.captures.get(cid)
  if cap is None or not cap.isOpened(): cap=cv2.VideoCapture(cam['rtsp_url']); self.captures[cid]=cap
  started=time.perf_counter(); ok,image=await asyncio.to_thread(cap.read); latency=round((time.perf_counter()-started)*1000)
  now=time.time()
  if now-self.last_telemetry.get(cid,0)>10:
   await self.post(f'/api/cameras/{cid}/telemetry',{'status':'online' if ok else 'offline','fps':float(cap.get(cv2.CAP_PROP_FPS) or 0),'latency_ms':latency}); self.last_telemetry[cid]=now
  if not ok or self.model is None: return
  result=(await asyncio.to_thread(self.model.predict,image,conf=CONF,device=DEVICE,verbose=False))[0]; detections=[]; stamp=datetime.now(timezone.utc).isoformat()
  for index,(xyxy,cls,score) in enumerate(zip(result.boxes.xyxy.cpu().tolist(),result.boxes.cls.cpu().tolist(),result.boxes.conf.cpu().tolist())):
   label=str(self.model.names[int(cls)])
   if label not in EVENT_CLASSES: continue
   x1,y1,x2,y2=xyxy; detections.append({'camera_id':cid,'model_name':self.model_name,'timestamp':stamp,'event_type':label,'confidence':score,'detection_id':f'{cid}:{int(time.time()*1000)}:{index}','bbox':[x1,y1,x2,y2]})
  if detections: await self.post('/api/inference/detections',{'detections':detections})
 async def run(self):
  if not WORKER_TOKEN: raise RuntimeError('ZMK_WORKER_TOKEN is required')
  while True:
   try:
    await self.load_model(); cameras=await self.get('/api/internal/cameras',True)
    for cam in cameras:
     await self.frame(cam); await asyncio.sleep(max(.01,1/float(cam['fps_limit'])))
    if not cameras: await asyncio.sleep(5)
   except Exception as exc:  # noqa: BLE001 - keep long-running worker alive
    print(f'inference loop error: {exc}',flush=True); await asyncio.sleep(3)
if __name__=='__main__': asyncio.run(Runtime().run())
