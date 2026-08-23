FROM ultralytics/ultralytics:8.4.126
WORKDIR /worker
# Use the standalone FFmpeg decoder for damaged RTP/H.264 streams. It has
# packet-discard/error-concealment controls that OpenCV VideoCapture lacks.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
# -u guarantees that camera state messages reach docker logs immediately.
CMD ["python", "-u", "main.py"]
