FROM python:3.12-slim
WORKDIR /worker
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir \
    ultralytics==8.4.126 \
    opencv-python-headless==4.10.0.84 \
    torch==2.5.1 torchvision==0.20.1 --extra-index-url https://download.pytorch.org/whl/cpu
COPY main.py .
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8010"]
