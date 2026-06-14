FROM rust:1.87-slim

RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    pkg-config \
    curl \
    git \
    bash \
    ca-certificates \
    cmake \
    python3 \
    nodejs \
    npm \
    jq \
    docker.io \
    && apt-get clean && rm -rf /var/lib/apt/lists/*


RUN curl -L https://foundry.paradigm.xyz | bash && \
    /root/.foundry/bin/foundryup && \
    /root/.foundry/bin/forge --version

ENV PATH="/root/.foundry/bin:/root/.risc0/bin:${PATH}"


WORKDIR /dfl
COPY smart_contracts ./smart_contracts
COPY data/phala_tdx_quote ./data/phala_tdx_quote
COPY data/initial_gm ./data/initial_gm
COPY data/rsa_keys ./data/rsa_keys
COPY data/rsa_keys/private_key.pem ./data/initial_gm/private_key.pem
COPY phala ./phala

RUN mkdir -p /dfl/smart_contracts/cache /dfl/smart_contracts/out /dfl/smart_contracts/broadcast

WORKDIR /dfl/smart_contracts
