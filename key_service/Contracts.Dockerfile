ARG CONTRACT_BASE=ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:19180092da9e8a7b4783fba589788d1269f2e216e01efb39e8021819901a32ad
FROM ${CONTRACT_BASE}
COPY smart_contracts/src /dfl/smart_contracts/src
COPY smart_contracts/script /dfl/smart_contracts/script
COPY smart_contracts/out /dfl/smart_contracts/out
COPY smart_contracts/starter_docker.sh /dfl/smart_contracts/starter_docker.sh
COPY data/dstack-0.5.12-reference-tdx-quote /dfl/data/dstack-0.5.12-reference-tdx-quote
COPY key_service/runtime_bootstrap.py /dfl/key_service/runtime_bootstrap.py
