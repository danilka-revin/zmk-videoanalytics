FROM python:3.12-alpine

# Use Alpine for fast apk install - docker-cli is small, unlike apt docker.io
# docker-cli + docker-cli-compose provides `docker compose` via mounted /var/run/docker.sock
# git is used for `git ls-remote` when looking up the head commit of the
# tracked branch: unlike the GitHub REST API it has no request quota, so the
# panel can check for new commits as often as it likes.
RUN apk add --no-cache curl git docker-cli docker-cli-compose

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8020
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8020"]
