FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        docker.io \
        docker-compose && \
    rm -rf /var/lib/apt/lists/*

COPY control_api ./control_api

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install -r control_api/requirements.txt

CMD ["python", "control_api/server.py"]
