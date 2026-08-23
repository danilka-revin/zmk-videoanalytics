FROM ultralytics/ultralytics:8.4.126
WORKDIR /worker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
# -u guarantees that camera state messages reach docker logs immediately.
CMD ["python", "-u", "main.py"]
