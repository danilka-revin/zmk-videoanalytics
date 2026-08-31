FROM python:3.12-slim
WORKDIR /worker
# OpenCV needs libgl and libglib for headless, ffmpeg for RTSP decoding like VLC
# Use opencv as default decoder for true FPS (direct H264, no MJPEG re-encode)
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Ultralytics + OpenCV + Torch CPU for inference, full quality for model
# Use CPU torch to keep image runnable without NVIDIA, GPU auto-detected via torch.cuda.is_available()
RUN pip install --no-cache-dir \
    ultralytics==8.4.126 \
    opencv-python-headless==4.10.0.84 \
    torch==2.5.1 torchvision==0.20.1 --extra-index-url https://download.pytorch.org/whl/cpu
COPY main.py .
# -u guarantees that camera state messages reach docker logs immediately.
CMD ["python", "-u", "main.py"]
