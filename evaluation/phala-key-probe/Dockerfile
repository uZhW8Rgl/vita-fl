ARG WORKER_BASE=ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:7428cf6a93b617f08bd5738b149911d7a55cf1b7c7824ecc94c0a59696e50384
FROM ${WORKER_BASE}
COPY evaluation/phala-key-probe /evaluation/phala-key-probe
ARG VARIANT=approved-a
RUN printf '%s\n' "${VARIANT}" > /evaluation/phala-key-probe/variant-id
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s CMD curl -fsS http://127.0.0.1:8001/health || exit 1
CMD ["node", "/evaluation/phala-key-probe/server.mjs"]
