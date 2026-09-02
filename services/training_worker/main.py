from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import queue
import shutil
from pathlib import Path

import cv2
import httpx
import torch
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from ultralytics import YOLO

API=os.getenv('ZMK_API_URL','http://api:8000').rstrip('/'); DEVICE_SETTING=os.getenv('TRAINING_DEVICE','auto'); DEVICE=(0 if torch.cuda.is_available() else 'cpu') if DEVICE_SETTING=='auto' else DEVICE_SETTING; KEY=os.getenv('ZMK_API_KEY',''); BASE_MODEL=os.getenv('BASE_TRAIN_MODEL','yolo11n.pt'); ROOT=Path(os.getenv('TRAINING_DATA_DIR','/data')); MODELS=Path(os.getenv('MODEL_DIR','/models'))
def service_token():
 path=Path(os.getenv('ZMK_BOT_API_TOKEN_FILE','/bot-secrets/.api-token'))
 try: return path.read_text(encoding='utf-8').strip() if path.is_file() else ''
 except OSError: return ''
app=FastAPI(title='ZMK Training Worker',version='1.0.0'); running:set[int]=set(); tasks:dict[int,asyncio.Task]={}
class Job(BaseModel):
 id:int; camera_id:str=''; rtsp_url:str=''; target_name:str; base_artifact:str|None=None; image_count:int=Field(ge=20,le=5000); epochs:int=Field(ge=1,le=300); fps_limit:float=Field(default=2,gt=0,le=10); batch:int=Field(default=8,ge=1,le=128); imgsz:int=Field(default=640,ge=320,le=1920); patience:int=Field(default=20,ge=0,le=100); confidence:float=Field(default=.35,ge=.05,le=.95); val_split:float=Field(default=.2,ge=.1,le=.4); source:str='camera'; dataset_path:str|None=None; dataset_kind:str='yolo'; frame_skip:int=Field(default=8,ge=1,le=120)
async def callback(job:int,**values):
 headers={'X-API-Key':KEY} if KEY else {}
 token=service_token()
 if token: headers['X-Bot-Service-Token']=token
 async with httpx.AsyncClient(base_url=API,headers=headers,timeout=30) as c: (await c.put(f'/api/training/jobs/{job}/progress',json=values)).raise_for_status()
def capture(job:Job,path:Path):
 cap=cv2.VideoCapture(job.rtsp_url); interval=max(1,int((cap.get(cv2.CAP_PROP_FPS) or 25)/job.fps_limit)); saved=frame=0
 while saved<job.image_count:
  ok,img=cap.read()
  if not ok: break
  if frame%interval==0: cv2.imwrite(str(path/f'{saved:06}.jpg'),img); saved+=1
  frame+=1
 cap.release()
 if saved<20: raise RuntimeError(f'Captured only {saved} frames; RTSP stream unavailable or too short')
IMAGE_EXTS={'.jpg','.jpeg','.png','.bmp','.webp','.tif','.tiff'}
VIDEO_EXTS={'.mp4','.avi','.mov','.mkv','.m4v','.webm','.mpg','.mpeg','.wmv'}

def _training_base_model(job:Job) -> str:
 """Prefer the active PyTorch PPE model when refining it on local data.

 ONNX artifacts are excellent for inference but cannot be resumed by
 Ultralytics' trainer, so in that case safely fall back to the standard base.
 """
 candidate=Path(job.base_artifact.removeprefix('file://')) if job.base_artifact else None
 if candidate and candidate.is_file() and candidate.suffix.lower()=='.pt': return str(candidate)
 return BASE_MODEL

def _common_train(data_yaml, job:Job, work:Path, updates=None):
 trainer=YOLO(_training_base_model(job)); updates and updates.put(('progress',60,'Обучение YOLO'))
 result=trainer.train(data=str(data_yaml),epochs=job.epochs,imgsz=job.imgsz,batch=job.batch,patience=job.patience,device=DEVICE,project=str(work/'runs'),name='train',exist_ok=True,verbose=False)
 updates and updates.put(('progress',90,'Экспорт ONNX'))
 best=work/'runs'/'train'/'weights'/'best.pt'; MODELS.mkdir(parents=True,exist_ok=True); target=MODELS/f'{job.target_name}.pt'; shutil.copy2(best,target); exported=YOLO(str(target)).export(format='onnx',dynamic=True,simplify=True); onnx=MODELS/f'{job.target_name}.onnx'; shutil.move(str(exported),onnx)
 metrics=getattr(result,'results_dict',{}); return onnx,float(metrics.get('metrics/precision(B)',0))*100,float(metrics.get('metrics/recall(B)',0))*100

def _load_pseudo_model(job:Job):
 base=job.base_artifact.removeprefix('file://') if job.base_artifact else BASE_MODEL
 return YOLO(base)

def _gather_images(root:Path):
 out=[]
 for p in root.rglob('*'):
  if p.is_file() and p.suffix.lower() in IMAGE_EXTS: out.append(p)
 return sorted(out)

def _gather_videos(root:Path):
 out=[]
 for p in root.rglob('*'):
  if p.is_file() and p.suffix.lower() in VIDEO_EXTS: out.append(p)
 return sorted(out)

def _pseudo_label(images, work:Path, pseudo_model, conf):
 images_dir=work/'images'/'all'; labels_dir=work/'labels'/'all'; images_dir.mkdir(parents=True); labels_dir.mkdir(parents=True)
 count=0
 for idx,image in enumerate(images):
  try:
   result=pseudo_model.predict(str(image),verbose=False,conf=conf)[0]
  except (RuntimeError,ValueError,OSError,TypeError):
   continue
  h,w=result.orig_shape; lines=[]
  for box,cls in zip(result.boxes.xywh.cpu().tolist(),result.boxes.cls.cpu().tolist()):
   x,y,bw,bh=box; lines.append(f'{int(cls)} {x/w:.6f} {y/h:.6f} {bw/w:.6f} {bh/h:.6f}')
  ext=image.suffix.lower() if image.suffix.lower() in {'.jpg','.jpeg','.png','.bmp','.webp'} else '.jpg'
  stem=f'{idx:06}'; shutil.copy2(image,images_dir/f'{stem}{ext}')
  if lines: (labels_dir/f'{stem}.txt').write_text('\n'.join(lines)); count+=1
 return count

def _finalize_dataset(work:Path, names, val_split):
 for split in ('train','val'): (work/'images'/split).mkdir(parents=True,exist_ok=True); (work/'labels'/split).mkdir(parents=True,exist_ok=True)
 labeled=sorted(p for p in (work/'labels'/'all').glob('*.txt'))
 if len(labeled)<10: raise RuntimeError('Pseudo-labeling produced fewer than 10 labeled frames')
 for index,label in enumerate(labeled):
  split='val' if index < max(1,int(len(labeled)*val_split)) else 'train'
  base_src=work/'images'/'all'
  matches=list(base_src.glob(f'{label.stem}.*'))
  if not matches: continue
  image=matches[0]
  shutil.move(str(image),work/'images'/split/image.name); shutil.move(str(label),work/'labels'/split/label.name)
 data=work/'data.yaml'; data.write_text(yaml.safe_dump({'path':str(work),'train':'images/train','val':'images/val','names':names},allow_unicode=True))
 return data

def _extract_frames(videos, work:Path, frame_skip:int):
 frames_dir=work/'frames'; frames_dir.mkdir(parents=True,exist_ok=True)
 saved=0
 for video in videos:
  cap=cv2.VideoCapture(str(video))
  i=0
  while True:
   ok,img=cap.read()
   if not ok: break
   if i%frame_skip==0:
    cv2.imwrite(str(frames_dir/f'v{saved:06}.jpg'),img); saved+=1
   i+=1
  cap.release()
 if saved==0: raise RuntimeError('No frames could be extracted from the provided videos')
 return sorted(frames_dir.glob('*.jpg'))

def train_images(job:Job, work:Path, updates=None):
 dpath=Path(job.dataset_path)
 if not dpath.is_dir(): raise RuntimeError(f'Dataset directory not found: {job.dataset_path}')
 images=_gather_images(dpath)
 if len(images)<10: raise RuntimeError(f'Found only {len(images)} images, need at least 10')
 updates and updates.put(('progress',25,'Фото загружены'))
 pseudo_model=_load_pseudo_model(job); updates and updates.put(('progress',45,'Псевдоразметка фото'))
 count=_pseudo_label(images,work,pseudo_model,job.confidence)
 updates and updates.put(('progress',55,f'Псевдоразметка: {count} кадров'))
 data=_finalize_dataset(work,pseudo_model.names,job.val_split)
 return _common_train(data,job,work,updates)

def train_videos(job:Job, work:Path, updates=None):
 dpath=Path(job.dataset_path)
 if not dpath.is_dir(): raise RuntimeError(f'Dataset directory not found: {job.dataset_path}')
 videos=_gather_videos(dpath)
 if not videos: raise RuntimeError('No video files found in the archive')
 updates and updates.put(('progress',20,'Видео загружены'))
 frames=_extract_frames(videos,work,job.frame_skip)
 updates and updates.put(('progress',40,f'Извлечено кадров: {len(frames)}'))
 pseudo_model=_load_pseudo_model(job); updates and updates.put(('progress',50,'Псевдоразметка кадров из видео'))
 count=_pseudo_label(frames,work,pseudo_model,job.confidence)
 updates and updates.put(('progress',60,f'Псевдоразметка: {count} кадров'))
 data=_finalize_dataset(work,pseudo_model.names,job.val_split)
 return _common_train(data,job,work,updates)

def train_finished_yolo(job:Job, work:Path, updates=None):
 dpath=Path(job.dataset_path)
 if not dpath.is_dir(): raise RuntimeError(f'Dataset directory not found: {job.dataset_path}')
 data_yaml=dpath/'data.yaml'
 if not data_yaml.is_file(): raise RuntimeError('Dataset has no data.yaml')
 cfg=yaml.safe_load(data_yaml.read_text(encoding='utf-8')) or {}
 names=cfg.get('names')
 if not names: raise RuntimeError("Dataset data.yaml has no 'names'")
 images_dir=dpath/'images'
 if not images_dir.is_dir() or not any(p.is_file() and p.suffix.lower() in IMAGE_EXTS for p in images_dir.rglob('*')):
  raise RuntimeError('Dataset missing images/ directory with training images')
 updates and updates.put(('progress',30,'Готовый датасет загружен'))
 updates and updates.put(('progress',45,'Начало обучения на готовом датасете'))
 return _common_train(str(data_yaml),job,work,updates)

def train_camera(job:Job, work:Path, updates=None):
 capture(job,work/'images'/'all'); updates and updates.put(('progress',25,'Кадры захвачены'))
 pseudo_model=_load_pseudo_model(job); updates and updates.put(('progress',40,'Псевдоразметка'))
 images=sorted((work/'images'/'all').glob('*.jpg'))
 count=_pseudo_label(images,work,pseudo_model,job.confidence)
 updates and updates.put(('progress',55,f'Псевдоразметка: {count} кадров'))
 data=_finalize_dataset(work,pseudo_model.names,job.val_split)
 return _common_train(data,job,work,updates)

def train(job:Job, updates=None):
 work=ROOT/f'job-{job.id}'; shutil.rmtree(work,ignore_errors=True)
 if job.source=='dataset':
  kind=(job.dataset_kind or 'yolo')
  if kind=='images': return train_images(job,work,updates)
  if kind=='videos': return train_videos(job,work,updates)
  return train_finished_yolo(job,work,updates)
 return train_camera(job,work,updates)

class PreviewRequest(BaseModel):
 dataset_path:str; base_artifact:str|None=None; confidence:float=Field(default=.35,ge=.05,le=.95); limit:int=Field(default=5,ge=1,le=12); kind:str='images'

def _to_list(value):
 try: value=value.cpu()
 except AttributeError: pass
 return value.tolist()

def _annotate_frame(result,image):
 for box,cls,score in zip(_to_list(result.boxes.xyxy),_to_list(result.boxes.cls),_to_list(result.boxes.conf)):
  x1,y1,x2,y2=map(int,box); label=f"{int(cls)} {score:.2f}"
  cv2.rectangle(image,(x1,y1),(x2,y2),(255,255,0),2)
  cv2.putText(image,label,(min(x1,max(image.shape[1]-140,0)),max(y1-5,14)),cv2.FONT_HERSHEY_SIMPLEX,.45,(255,255,0),1,cv2.LINE_AA)
 return image

def preview_dataset(req:PreviewRequest):
 import base64 as _b64
 dpath=Path(req.dataset_path)
 if not dpath.is_dir(): raise RuntimeError(f'Dataset directory not found: {req.dataset_path}')
 model_src=req.base_artifact.removeprefix('file://') if req.base_artifact else BASE_MODEL
 pseudo_model=YOLO(model_src)
 frames=[]
 if req.kind=='videos':
  videos=_gather_videos(dpath)
  if not videos: raise RuntimeError('No video files found')
  for video in videos:
   cap=cv2.VideoCapture(str(video)); i=0; step=30; got=0
   while got<req.limit and i<400:
    ok,img=cap.read()
    if not ok: break
    if i%step==0: frames.append((video.name,img)); got+=1
    i+=1
   cap.release()
   if len(frames)>=req.limit: break
 else:
  for p in _gather_images(dpath)[:req.limit]:
   img=cv2.imread(str(p))
   if img is not None: frames.append((p.name,img))
 results=[]
 for source,img in frames:
  if img is None: continue
  try: result=pseudo_model.predict(img,verbose=False,conf=req.confidence)[0]
  except (RuntimeError,ValueError,OSError,TypeError): continue
  annotated=_annotate_frame(result,img.copy())
  ok,buf=cv2.imencode('.jpg',annotated)
  if not ok: continue
  n_obj=len(_to_list(result.boxes.xyxy))
  results.append({'source':source,'label':f'{n_obj} объектов','image':_b64.b64encode(buf).decode()})
  if len(results)>=req.limit: break
 if not results: raise RuntimeError('Псевдоразметка не нашла объектов для предпросмотра')
 return {'count':len(results),'items':results}

def train_entry(payload, updates):
 try:
  artifact,precision,recall=train(Job(**payload),updates); updates.put(('success',str(artifact),precision,recall))
 except Exception as exc:  # noqa: BLE001 - transfer child process failure
  updates.put(('error',str(exc)[:500]))
async def execute(job:Job):
 running.add(job.id); ctx=mp.get_context('spawn'); updates=ctx.Queue(); process=ctx.Process(target=train_entry,args=(job.model_dump(),updates),daemon=True); process.start()
 try:
  await callback(job.id,status='running',progress=5,stage='Обучение на готовом датасете' if job.source=='dataset' else 'Захват RTSP кадров')
  empty_after_exit=0
  while True:
   try: message=await asyncio.to_thread(updates.get,True,1)
   except queue.Empty:
    if process.is_alive(): continue
    empty_after_exit+=1
    if empty_after_exit>=2: break
    continue
   empty_after_exit=0
   if message[0]=='progress': await callback(job.id,status='running',progress=message[1],stage=message[2])
   elif message[0]=='error': raise RuntimeError(message[1])
   elif message[0]=='success': await callback(job.id,status='completed',progress=100,stage='Модель обучена',artifact_uri=f'file://{message[1]}',precision=message[2],recall=message[3]); return
  raise RuntimeError(f'Training process exited without result (code {process.exitcode})')
 except asyncio.CancelledError:
  if process.is_alive(): process.terminate(); process.join(timeout=10)
  await callback(job.id,status='cancelled',progress=0,stage='Отменено'); raise
 except Exception as exc:  # noqa: BLE001 - report all ML/RTSP/CUDA failures
  if process.is_alive(): process.terminate(); process.join(timeout=10)
  await callback(job.id,status='failed',progress=0,stage='Ошибка',error=str(exc)[:500])
 finally:
  if process.is_alive(): process.terminate()
  process.join(timeout=5); running.discard(job.id); tasks.pop(job.id,None); updates.close()
@app.get('/health')
def health(): return {'status':'ok','gpu':torch.cuda.is_available(),'device':str(DEVICE),'running_jobs':list(running)}
@app.post('/preview')
def preview(req:PreviewRequest):
 try: return preview_dataset(req)
 except RuntimeError as exc: raise HTTPException(400,str(exc)) from exc
@app.post('/jobs',status_code=202)
async def jobs(job:Job):
 if running: raise HTTPException(409,'Training worker is busy')
 task=asyncio.create_task(execute(job),name=f'training-{job.id}'); tasks[job.id]=task; return {'accepted':True,'job_id':job.id}

@app.delete('/jobs/{job_id}')
async def cancel(job_id:int):
 task=tasks.get(job_id)
 if not task: raise HTTPException(404,'Job is not running')
 task.cancel(); return {'cancelled':True,'job_id':job_id}
