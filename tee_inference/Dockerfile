FROM smallstep/step-cli:0.28.7 AS step-cli

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    DATASET_NAME=chestmnist \
    TEE_MODEL_DIR=/tmp/tee-inference/model \
    TEE_JOB_DIR=/tmp/tee-inference/jobs \
    CHESTMNIST_TEST_DATA=/app/data/chestmnist/test_data/test-data.npz

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl libgomp1 openssl nginx && \
    rm -rf /var/lib/apt/lists/*

COPY tee_inference/requirements.lock /tmp/requirements.lock
RUN python -m pip install --upgrade pip && \
    python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.8.0 && \
    python -m pip install -r /tmp/requirements.lock && \
    rm /tmp/requirements.lock

COPY tee_inference /app/tee_inference
COPY agent_receipts /app/agent_receipts
COPY transport_security /app/transport_security
COPY pki /app/pki
COPY --from=step-cli /usr/local/bin/step /usr/local/bin/step
COPY dfl/neural_network /app/dfl/neural_network
COPY agent/blockchain_source.py agent/ipfs_bundle.py /app/agent/
COPY data/chestmnist/test_data/test-data.npz /app/data/chestmnist/test_data/test-data.npz
RUN mkdir -p /tmp/tee-inference/model /tmp/tee-inference/jobs

EXPOSE 8443

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=4 \
    CMD curl --fail --silent --unix-socket /run/vita-fl/inference.sock http://localhost/healthz >/dev/null || exit 1

CMD ["python", "-m", "pki.runtime", "run", "worker", "--", "python", "-m", "tee_inference.service"]
