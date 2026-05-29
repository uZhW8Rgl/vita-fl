FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DEFAULT_TIMEOUT=120
ENV PYTHONPATH=/app:/app/zk_inference

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY zk_inference ./zk_inference
COPY dfl/neural_network ./dfl/neural_network
COPY data ./data

RUN python -m pip install --upgrade pip setuptools wheel && \
    attempts=0 && \
    until python -m pip install --index-url https://download.pytorch.org/whl/cpu torch; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "zk torch install failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "zk torch install failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done && \
    attempts=0 && \
    until python -m pip install -r zk_inference/requirements.txt; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "zk requirements install failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "zk requirements install failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done && \
    python -m pip install -e dfl/neural_network && \
    attempts=0 && \
    until python -m pip install ezkl; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "ezkl install failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "ezkl install failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done

EXPOSE 8090

CMD ["python", "zk_inference/server.py"]
