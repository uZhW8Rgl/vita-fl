ARG WORKER_BASE=ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:af7edbbbdd890fce60ac7a2abbf8e205671665903945d4483ccc94ed78f31c5f
FROM ${WORKER_BASE}
COPY evaluation/live-role /evaluation/live-role
ARG VARIANT=approved
RUN printf '%s\n' "$VARIANT" > /evaluation/live-role/variant-id
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s CMD curl -fsS http://127.0.0.1:8001/health || exit 1
CMD ["bash", "-ec", "ulimit -c 0; exec node /evaluation/live-role/server.mjs"]
