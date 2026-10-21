ARG WORKER_BASE=ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:af7edbbbdd890fce60ac7a2abbf8e205671665903945d4483ccc94ed78f31c5f
FROM ${WORKER_BASE}
COPY dfl/node_server/dist /dfl/node_server/dist
COPY dfl/node_server/abi /dfl/node_server/abi
COPY agent_receipts /agent_receipts
COPY dfl/start_node_neural_network.sh /dfl/start_node_neural_network.sh
COPY evaluation/phala-broker-probe /evaluation/phala-broker-probe
ARG VARIANT=original
RUN printf '%s\n' "${VARIANT}" > /evaluation/phala-broker-probe/variant
