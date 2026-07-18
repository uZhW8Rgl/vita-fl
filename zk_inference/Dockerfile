FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DEFAULT_TIMEOUT=120
ENV PYTHONPATH=/app:/app/zk_inference

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY zk_inference/requirements.txt /tmp/zk-inference-requirements.txt

RUN python -m pip install --upgrade pip setuptools wheel && \
    attempts=0 && \
    until python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.8.0; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "zk torch install failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "zk torch install failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done && \
    attempts=0 && \
    until python -m pip install -r /tmp/zk-inference-requirements.txt; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "zk requirements install failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "zk requirements install failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done && \
    attempts=0 && \
    until python -m pip install ezkl==23.0.5; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "ezkl install failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "ezkl install failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done

COPY zk_inference ./zk_inference
COPY agent_receipts ./agent_receipts
COPY dfl/neural_network ./dfl/neural_network
COPY data/mnist_tools.py ./data/mnist_tools.py
COPY data/chestmnist/test_data/test-data.npz ./data/chestmnist/test_data/test-data.npz
COPY tee_inference/protocol ./tee_inference/protocol
COPY tee_inference/service/__init__.py tee_inference/service/model_source.py ./tee_inference/service/
COPY agent/blockchain_source.py agent/ipfs_bundle.py ./agent/

RUN python -m pip install -e dfl/neural_network

EXPOSE 8090

RUN mkdir -p /work/zk-inference && chmod 0700 /work/zk-inference

CMD ["python", "zk_inference/server.py"]
