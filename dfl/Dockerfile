FROM node:20-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DEFAULT_TIMEOUT=120
ENV THESIS_NODE_SERVER_DIR=/dfl/node_server
ENV PYTHON_SERVICE_HOST=0.0.0.0
ENV PYTHON_SERVICE_PORT=8000
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
        python3 \
        python3-venv \
        python3-pip && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/python -m pip install --upgrade pip && \
    attempts=0 && \
    until /opt/venv/bin/python -m pip install cryptography numpy pyzmq; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "pip base deps failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "pip base deps failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done && \
    attempts=0 && \
    until /opt/venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch; do \
        attempts=$((attempts + 1)); \
        if [ "$attempts" -ge 3 ]; then \
            echo "pip torch failed after ${attempts} attempts"; \
            exit 1; \
        fi; \
        echo "pip torch failed (attempt ${attempts}/3), retrying..."; \
        sleep 5; \
    done

ENV PATH="/opt/venv/bin:${PATH}"

COPY data/mnist ./config
COPY data/chestmnist ./config/chestmnist
COPY data/initial_gm ./initial_gm
COPY data/rsa_keys ./keys

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

WORKDIR /dfl/node_server
RUN npm run build

COPY data/mnist/data/train-images.idx3-ubyte ./node_server/data/train-images.idx3-ubyte
COPY data/mnist/data/train-labels.idx1-ubyte ./node_server/data/train-labels.idx1-ubyte
COPY data/mnist/data/t10k-images.idx3-ubyte ./node_server/data/t10k-images.idx3-ubyte
COPY data/mnist/data/t10k-labels.idx1-ubyte ./node_server/data/t10k-labels.idx1-ubyte
COPY data/phala_tdx_quote ./attestation/

RUN chmod +x /dfl/start_node_neural_network.sh && \
    mkdir -p /dfl/node_server/data/results_iid /dfl/node_server/received_models /dfl/node_server/attestation

WORKDIR /dfl/node_server

EXPOSE 8000 5555

CMD ["node", "/dfl/node_server/dist/server.js"]
