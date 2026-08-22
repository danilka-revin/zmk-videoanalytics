from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import httpx
import torch
from ultralytics import YOLO

API=os.getenv('ZMK_API_URL','http://api:8000').rstrip('/'); API_KEY=os.getenv('ZMK_API_KEY',''); DEVICE_SETTING=os.getenv('INFERENCE_DEVICE','auto'); DEVICE=('0' if torch.cuda.is_available() else 'cpu') if DEVICE_SETTING=='auto' else DEVICE_SETTING; CONF=float(os.getenv('INFERENCE_CONF','0.5'))
def _worker_token():
 tok=os.getenv('ZMK_WORKER_TOKEN','').strip()
 if tok: return tok
 f=Path(os.getenv('ZMK_WORKER_TOKEN_FILE','/models/.worker-token'))
 try:
  if f.is_file():
   t=f.read_text(encoding='utf-8').strip()
   if t: return t
 except OSError: pass
 return ''
WORKER_TOKEN=_worker_token()
# RTSP transport handling. "auto" (default) tries TCP first, then falls back
# to UDP on failure -> solves "works in VLC but not here" because VLC may use a
# transport that OpenCV/FFmpeg rejects. Fixed values disable the fallback.
RTSP_TRANSPORT=os.getenv('RTSP_TRANSPORT','auto').lower()
if RTSP_TRANSPORT not in ('auto','tcp','udp'): RTSP_TRANSPORT='auto'
TRANSPORT_ORDER=['tcp','udp'] if RTSP_TRANSPORT=='auto' else [RTSP_TRANSPORT]
# Optional FFmpeg buffer size; only set if the operator explicitly asks.
_RTSP_BUFSIZE=os.getenv('RTSP_BUFFER_SIZE','').strip()
# Explicit RTSP socket timeout (ms) passed to FFmpeg. Prevents the default
# ~30s "Stream timeout" stall in containers.
_RTSP_STIMEOUT=int(os.getenv('RTSP_STIMEOUT','5000000'))
OFFLINE_AFTER=int(os.getenv('OFFLINE_AFTER_FRAMES','3'))  # consecutive failed reads before "offline"
RECONNECT_MIN=int(os.getenv('RTSP_RECONNECT_SECONDS','5'))
EVENT_CLASSES={'no_helmet','no_vest','phone_usage','smoking','restricted_zone','immobility'}
def file_sha256(path:Path):
 hasher=hashlib.sha256()
 with path.open('rb') as stream:
  for chunk in iter(lambda:stream.read(1024*1024),b''): hasher.update(chunk)
 return hasher.hexdigest()
class Runtime:
 def __init__(self): self.model=None; self.model_name=''; self.captures={}; self.last_telemetry={}; self.last_snapshot={}; self.frame_counts={}; self.last_error={}; self.fail_counts={}; self.next_open={}; self.transport={}; self.open_attempts={}
 async def get(self,path,internal=False):
  # Re-resolve the worker token on EVERY internal call: the token lives on
  # the shared model-data volume and may be provisioned/rotated after this
  # worker started (or the api container may create it first). Caching it at
  # import time is what produced constant 401s when the worker started before
  # the api had written /models/.worker-token.
  token=WORKER_TOKEN
  if internal:
   token=_worker_token()
  headers={'X-Worker-Token':token} if internal else ({'X-API-Key':API_KEY} if API_KEY else {})
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
 def _next_transport(self,cid):
  # rotate through TRANSPORT_ORDER so a fixed mode stays fixed and "auto"
  # alternates tcp/udp on each reconnect attempt.
  cur=self.transport.get(cid,TRANSPORT_ORDER[0])
  try: idx=TRANSPORT_ORDER.index(cur)
  except ValueError: idx=0
  nxt=TRANSPORT_ORDER[(idx+1)%len(TRANSPORT_ORDER)]
  self.transport[cid]=nxt
  return nxt
 def _open_capture(self,url,transport):
  # OpenCV reads OPENCV_FFMPEG_CAPTURE_OPTIONS as "key;value" pairs joined by
  # '|' (NOT a comma). Using a comma (we once did) makes the RTSP demuxer
  # reject rtsp_transport entirely ("Invalid chars ... at the end of
  # expression"), so BOTH tcp/udp fail and the camera never opens.
  # Correct form: 'rtsp_transport;tcp|stimeout;5000000[|buffer_size;N]'.
  # stimeout gives the RTSP socket an explicit timeout, which avoids the
  # default ~30s stall (Stream timeout) in containers.
  parts=[f'rtsp_transport;{transport}',f'stimeout;{_RTSP_STIMEOUT}']
  if _RTSP_BUFSIZE:
   parts.append(f'buffer_size;{_RTSP_BUFSIZE}')
  os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']='|'.join(parts)
  cap=cv2.VideoCapture(url,cv2.CAP_FFMPEG)
  cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,8000)
  cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC,8000)
  return cap
 async def frame(self,cam):
  cid=cam['id']; now=time.time()
  cap=self.captures.get(cid)
  # (Re)open, respecting a minimum reconnect interval so a bad stream isn't
  # hammered with thousands of open attempts per second.
  if cap is None or not cap.isOpened():
   if now < self.next_open.get(cid,0):
    await asyncio.sleep(.2); return
   old_cap=self.captures.pop(cid,None)
   if old_cap is not None:
    try: old_cap.release()
    except (RuntimeError,OSError,ValueError): pass
   transport=self._next_transport(cid); self.open_attempts[cid]=self.open_attempts.get(cid,0)+1
   cap=self._open_capture(cam['rtsp_url'],transport)
   if not cap.isOpened():
    self.next_open[cid]=now+RECONNECT_MIN
    print(f'inference: camera {cid}: FAILED to open via {transport} ({cam["name"]}) - will retry in {RECONNECT_MIN}s (transport mode: {RTSP_TRANSPORT}; attempts: {self.open_attempts[cid]})',flush=True)
    return
   if (self.open_attempts.get(cid,0)>1) or (not self.transport.get(cid)):
    print(f'inference: camera {cid}: OPENED via {transport} ({cam["name"]})',flush=True)
   self.captures[cid]=cap
  started=time.perf_counter(); ok,image=await asyncio.to_thread(cap.read); latency=round((time.perf_counter()-started)*1000)
  fail=self.fail_counts.get(cid,0)
  if not ok or image is None:
   fail+=1; self.fail_counts[cid]=fail
   if fail>=OFFLINE_AFTER:
    # stream is dead: drop the capture so the next frame reopens it.
    try: self.captures.pop(cid,None).release()
    except (RuntimeError,OSError,ValueError): self.captures.pop(cid,None)
    self.last_telemetry.pop(cid,None)
    self.next_open[cid]=now+RECONNECT_MIN
    if now-self.last_error.get(cid,0)>15:
     print(f'inference: camera {cid}: stream lost ({cam["name"]}) after {fail} reads (transport={self.transport.get(cid,"?")}) - reconnecting',flush=True)
     self.last_error[cid]=now
  else:
   fail=0; self.fail_counts[cid]=0
   if cid in self.last_error: self.last_error.pop(cid,None)
  # Report telemetry. Status goes offline only after OFFLINE_AFTER consecutive
  # bad reads, to avoid constant online/offline flapping on a single dropped frame.
  if now-self.last_telemetry.get(cid,now)>10 or cid not in self.last_telemetry:
   status='online' if fail==0 else ('offline' if fail>=OFFLINE_AFTER else 'recovering')
   elapsed=max(.001,now-self.last_telemetry.get(cid,now)); effective=self.frame_counts[cid]/elapsed if cid in self.last_telemetry else 0
   try: await self.post(f'/api/cameras/{cid}/telemetry',{'status':status,'fps':effective,'latency_ms':latency})
   except (RuntimeError,OSError,ValueError): pass
   self.last_telemetry[cid]=now; self.frame_counts[cid]=0
  if fail>0 or self.model is None: return
  if now-self.last_snapshot.get(cid,0)>5:
   height,width=image.shape[:2]
   if width>960: image=cv2.resize(image,(960,int(height*960/width)))
   encoded_ok,encoded=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,75])
   if encoded_ok:
    try: await self.post(f'/api/cameras/{cid}/snapshot',{'jpeg_base64':base64.b64encode(encoded).decode(),'captured_at':datetime.now(timezone.utc).isoformat()})
    except (RuntimeError,OSError,ValueError): pass
   self.last_snapshot[cid]=now
  result=(await asyncio.to_thread(self.model.predict,image,conf=CONF,device=DEVICE,verbose=False))[0]; detections=[]; stamp=datetime.now(timezone.utc).isoformat()
  for index,(xyxy,cls,score) in enumerate(zip(result.boxes.xyxy.cpu().tolist(),result.boxes.cls.cpu().tolist(),result.boxes.conf.cpu().tolist())):
   label=str(self.model.names[int(cls)])
   if label not in EVENT_CLASSES: continue
   x1,y1,x2,y2=xyxy; spatial_id=f'{cid}-{label}-{int(((x1+x2)/2)//100)}-{int(((y1+y2)/2)//100)}'; detections.append({'camera_id':cid,'model_name':self.model_name,'timestamp':stamp,'event_type':label,'confidence':score,'person_id':spatial_id,'detection_id':f'{cid}:{int(time.time()*1000)}:{index}','bbox':[x1,y1,x2,y2]})
  if detections: await self.post('/api/inference/detections',{'detections':detections})
 async def run(self):
  if not WORKER_TOKEN: raise RuntimeError('ZMK_WORKER_TOKEN is required')
  while True:
   try:
    await self.load_model(); cameras=await self.get('/api/internal/cameras',True); active_ids={c['id'] for c in cameras}
    for stale in set(self.captures)-active_ids: self.captures.pop(stale).release(); self.last_telemetry.pop(stale,None); self.last_snapshot.pop(stale,None); self.frame_counts.pop(stale,None)
    for cam in cameras:
     await self.frame(cam); await asyncio.sleep(max(.01,1/float(cam['fps_limit'])))
    if not cameras: await asyncio.sleep(5)
   except Exception as exc:  # noqa: BLE001 - keep long-running worker alive
    print(f'inference loop error: {exc}',flush=True); await asyncio.sleep(3)
if __name__=='__main__': asyncio.run(Runtime().run())
