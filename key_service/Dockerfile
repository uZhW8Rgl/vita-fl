FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
WORKDIR /app
COPY dfl/node_server/package*.json ./dfl/node_server/
RUN cd dfl/node_server && npm ci --ignore-scripts
COPY dfl/node_server/src ./dfl/node_server/src
COPY dfl/node_server/tsconfig.json ./dfl/node_server/
RUN cd dfl/node_server && npm run build && npm prune --omit=dev
COPY key_service/requirements.txt ./key_service/
RUN pip install --no-cache-dir -r key_service/requirements.txt
COPY key_service ./key_service
COPY transport_security ./transport_security
ENV PYTHONPATH=/app KEY_SERVICE_HOST=0.0.0.0 KEY_SERVICE_PORT=8000
USER 10001:10001
CMD ["node", "key_service/runtime.mjs"]
