FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    DATASET_NAME=chestmnist \
    TEE_MODEL_PATH=/app/model/aggregated.bin \
    TEE_MODEL_MANIFEST_PATH=/app/model/model-manifest.cbor

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl libgomp1 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system --gid 10001 tee-inference && \
    useradd --system --uid 10001 --gid tee-inference --home-dir /app tee-inference

COPY tee_inference/requirements.lock /tmp/requirements.lock
RUN python -m pip install --upgrade pip && \
    python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.8.0 && \
    python -m pip install -r /tmp/requirements.lock && \
    rm /tmp/requirements.lock

COPY --chown=tee-inference:tee-inference tee_inference /app/tee_inference
COPY --chown=tee-inference:tee-inference dfl/neural_network /app/dfl/neural_network
RUN mkdir -p /app/model && chown tee-inference:tee-inference /app/model

USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=4 \
    CMD curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null || exit 1

CMD ["python", "-m", "tee_inference.service"]
