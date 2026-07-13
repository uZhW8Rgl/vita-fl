FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DEFAULT_TIMEOUT=120
ENV PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl && \
    rm -rf /var/lib/apt/lists/*

COPY agent ./agent
COPY data/mnist ./data/mnist
COPY data/chestmnist/test_data/test-data.npz ./data/chestmnist/test_data/test-data.npz
COPY tee_inference/air ./tee_inference/air
COPY tee_inference/protocol ./tee_inference/protocol

RUN python -m pip install --upgrade pip setuptools wheel && \
    attempts=0 && \
    until python -m pip install -r agent/requirements.txt; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "agent pip install failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "agent pip install failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done

CMD ["python", "agent/run_agent.py", "--serve", "--llm", "--source", "contract", "--rpc-url", "http://anvil:8545", "--gm-storage-address", "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0", "--registry-address", "0x5FbDB2315678afecb367f032d93F642f64180aa3", "--ipfs-api-url", "http://ipfs:5001", "--skip-calibration"]
