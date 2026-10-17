FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0
WORKDIR /app
COPY dfl/node_server/package.json dfl/node_server/package-lock.json ./dfl/node_server/
RUN cd dfl/node_server && npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY kms/boot-auth.mjs ./kms/
USER node
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["node", "kms/boot-auth.mjs"]
