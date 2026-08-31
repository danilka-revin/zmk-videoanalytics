FROM ultralytics/ultralytics:8.4.126
WORKDIR /worker
# ffmpeg for RTSP decoding like VLC (opencv decoder default gives true FPS)
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
# -u guarantees that camera state messages reach docker logs immediately.
CMD ["python", "-u", "main.py"]
