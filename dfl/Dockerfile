FROM node:20-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DEFAULT_TIMEOUT=120
ENV PYTHONPATH=/app
ENV THESIS_NODE_SERVER_DIR=/dfl/node_server
ENV PYTHON_SERVICE_HOST=127.0.0.1
ENV PYTHON_SERVICE_PORT=8000
ENV TEE_MODEL_DIR=/tmp/tee-inference/model
ENV TEE_JOB_DIR=/tmp/tee-inference/jobs
ENV CHESTMNIST_TEST_DATA=/dfl/config/chestmnist/test_data/test-data.npz
ENV NPM_CONFIG_FETCH_RETRIES=5
ENV NPM_CONFIG_FETCH_RETRY_FACTOR=2
ENV NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000
ENV NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000
ENV NPM_CONFIG_REGISTRY=https://registry.npmjs.org/

WORKDIR /dfl

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        bash \
        curl \
        libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY tee_inference/requirements.lock /tmp/tee-inference-requirements.lock

RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/python -m pip install --upgrade pip && \
    attempts=0 && \
    until /opt/venv/bin/python -m pip install \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.8.0; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "pip torch failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "pip torch failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done && \
    attempts=0 && \
    until /opt/venv/bin/python -m pip install \
        -r /tmp/tee-inference-requirements.lock \
        pyzmq; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "pip worker and inference dependencies failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "pip worker and inference dependencies failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done && \
    rm /tmp/tee-inference-requirements.lock

ENV PATH="/opt/venv/bin:${PATH}"

COPY data/mnist ./config
COPY data/chestmnist/training_data ./config/chestmnist/training_data
COPY data/chestmnist/test_data ./config/chestmnist/test_data
COPY data/chestmnist/README.md ./config/chestmnist/README.md
COPY data/initial_gm ./initial_gm

COPY dfl/node_server/package*.json ./node_server/
WORKDIR /dfl/node_server
RUN attempts=0 && \
    until npm ci; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "npm ci failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "npm ci failed (attempt ${attempts}/3), retrying..."; \
        sleep 10; \
    done

WORKDIR /dfl
COPY dfl/node_server ./node_server
COPY dfl/neural_network ./neural_network
COPY dfl/start_node_neural_network.sh ./start_node_neural_network.sh
COPY dfl/container_healthcheck.sh ./container_healthcheck.sh

WORKDIR /app
COPY tee_inference ./tee_inference
COPY agent_receipts ./agent_receipts
COPY agent/blockchain_source.py agent/ipfs_bundle.py ./agent/
COPY dfl/neural_network ./dfl/neural_network

WORKDIR /dfl/node_server
RUN npm run build

COPY data/mnist/data/train-images.idx3-ubyte ./node_server/data/train-images.idx3-ubyte
COPY data/mnist/data/train-labels.idx1-ubyte ./node_server/data/train-labels.idx1-ubyte
COPY data/mnist/data/t10k-images.idx3-ubyte ./node_server/data/t10k-images.idx3-ubyte
COPY data/mnist/data/t10k-labels.idx1-ubyte ./node_server/data/t10k-labels.idx1-ubyte
COPY data/phala_tdx_quote ./attestation/

RUN chmod +x /dfl/start_node_neural_network.sh /dfl/container_healthcheck.sh && \
    mkdir -p \
        /dfl/node_server/data/results_iid \
        /dfl/node_server/received_models \
        /dfl/node_server/attestation \
        /tmp/tee-inference/model \
        /tmp/tee-inference/jobs

WORKDIR /dfl/node_server

EXPOSE 8001 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=4 \
    CMD ["/dfl/container_healthcheck.sh"]

CMD ["/dfl/start_node_neural_network.sh"]
