from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import cv2
import httpx
import torch
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from ultralytics import YOLO

API=os.getenv('ZMK_API_URL','http://api:8000').rstrip('/'); KEY=os.getenv('ZMK_API_KEY',''); BASE_MODEL=os.getenv('BASE_TRAIN_MODEL','yolo11n.pt'); ROOT=Path(os.getenv('TRAINING_DATA_DIR','/data')); MODELS=Path(os.getenv('MODEL_DIR','/models'))
app=FastAPI(title='ZMK Training Worker',version='1.0.0'); running:set[int]=set(); tasks:dict[int,asyncio.Task]={}
class Job(BaseModel):
 id:int; camera_id:str; rtsp_url:str; target_name:str; base_artifact:str|None=None; image_count:int=Field(ge=20,le=5000); epochs:int=Field(ge=1,le=300); fps_limit:float=Field(default=2,gt=0,le=10); batch:int=Field(default=8,ge=1,le=128); imgsz:int=Field(default=640,ge=320,le=1920); patience:int=Field(default=20,ge=0,le=100); confidence:float=Field(default=.35,ge=.05,le=.95); val_split:float=Field(default=.2,ge=.1,le=.4)
async def callback(job:int,**values):
 async with httpx.AsyncClient(base_url=API,headers={'X-API-Key':KEY} if KEY else {},timeout=30) as c: (await c.put(f'/api/training/jobs/{job}/progress',json=values)).raise_for_status()
def capture(job:Job,path:Path):
 cap=cv2.VideoCapture(job.rtsp_url); interval=max(1,int((cap.get(cv2.CAP_PROP_FPS) or 25)/job.fps_limit)); saved=frame=0
 while saved<job.image_count:
  ok,img=cap.read()
  if not ok: break
  if frame%interval==0: cv2.imwrite(str(path/f'{saved:06}.jpg'),img); saved+=1
  frame+=1
 cap.release()
 if saved<20: raise RuntimeError(f'Captured only {saved} frames; RTSP stream unavailable or too short')
def train(job:Job):
 work=ROOT/f'job-{job.id}'; shutil.rmtree(work,ignore_errors=True); images=work/'images'/'all'; labels=work/'labels'/'all'; images.mkdir(parents=True); labels.mkdir(parents=True)
 capture(job,images); base=job.base_artifact.removeprefix('file://') if job.base_artifact else BASE_MODEL; pseudo_model=YOLO(base)
 count=0
 for image in images.glob('*.jpg'):
  result=pseudo_model.predict(str(image),verbose=False,conf=job.confidence)[0]; h,w=result.orig_shape; lines=[]
  for box,cls in zip(result.boxes.xywh.cpu().tolist(),result.boxes.cls.cpu().tolist()):
   x,y,bw,bh=box; lines.append(f'{int(cls)} {x/w:.6f} {y/h:.6f} {bw/w:.6f} {bh/h:.6f}')
  if lines: (labels/f'{image.stem}.txt').write_text('\n'.join(lines)); count+=1
 if count<10: raise RuntimeError('Pseudo-labeling produced fewer than 10 labeled frames')
 for split in ('train','val'): (work/'images'/split).mkdir(); (work/'labels'/split).mkdir()
 labeled=sorted(labels.glob('*.txt'))
 for index,label in enumerate(labeled):
  split='val' if index < max(1,int(len(labeled)*job.val_split)) else 'train'; image=images/f'{label.stem}.jpg'; shutil.move(str(image),work/'images'/split/image.name); shutil.move(str(label),work/'labels'/split/label.name)
 data=work/'data.yaml'; data.write_text(yaml.safe_dump({'path':str(work),'train':'images/train','val':'images/val','names':pseudo_model.names},allow_unicode=True))
 trainer=YOLO(BASE_MODEL); result=trainer.train(data=str(data),epochs=job.epochs,imgsz=job.imgsz,batch=job.batch,patience=job.patience,device=0,project=str(work/'runs'),name='train',exist_ok=True,verbose=False)
 best=work/'runs'/'train'/'weights'/'best.pt'; MODELS.mkdir(parents=True,exist_ok=True); target=MODELS/f'{job.target_name}.pt'; shutil.copy2(best,target); exported=YOLO(str(target)).export(format='onnx',dynamic=True,simplify=True); onnx=MODELS/f'{job.target_name}.onnx'; shutil.move(str(exported),onnx)
 metrics=getattr(result,'results_dict',{}); return onnx,float(metrics.get('metrics/precision(B)',0))*100,float(metrics.get('metrics/recall(B)',0))*100
async def execute(job:Job):
 running.add(job.id)
 try:
  await callback(job.id,status='running',progress=5,stage='Захват RTSP кадров'); artifact,precision,recall=await asyncio.to_thread(train,job)
  await callback(job.id,status='completed',progress=100,stage='Модель обучена',artifact_uri=f'file://{artifact}',precision=precision,recall=recall)
 except asyncio.CancelledError: await callback(job.id,status='cancelled',progress=0,stage='Отменено'); raise
 except Exception as exc:  # noqa: BLE001 - report all ML/RTSP/CUDA failures
  await callback(job.id,status='failed',progress=0,stage='Ошибка',error=str(exc)[:500])
 finally: running.discard(job.id); tasks.pop(job.id,None)
@app.get('/health')
def health(): return {'status':'ok','gpu':torch.cuda.is_available(),'running_jobs':list(running)}
@app.post('/jobs',status_code=202)
async def jobs(job:Job):
 if running: raise HTTPException(409,'Training worker is busy')
 task=asyncio.create_task(execute(job),name=f'training-{job.id}'); tasks[job.id]=task; return {'accepted':True,'job_id':job.id}

@app.delete('/jobs/{job_id}')
async def cancel(job_id:int):
 task=tasks.get(job_id)
 if not task: raise HTTPException(404,'Job is not running')
 task.cancel(); return {'cancelled':True,'job_id':job_id}
