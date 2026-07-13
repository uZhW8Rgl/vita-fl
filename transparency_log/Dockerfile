ARG AZURE_LINUX_TAG=3.0.20260616
ARG CCF_VERSION=7.0.6
ARG SCITT_COMMIT=b6ebc0518b59795147538bcc0cc7f67079a82d63

FROM mcr.microsoft.com/azurelinux/base/core:${AZURE_LINUX_TAG} AS source
ARG SCITT_COMMIT
RUN tdnf install -y ca-certificates git && \
    git clone https://github.com/microsoft/scitt-ccf-ledger.git /src && \
    cd /src && \
    git checkout --detach "${SCITT_COMMIT}" && \
    test "$(git rev-parse HEAD)" = "${SCITT_COMMIT}" && \
    rm -rf /src/.git

FROM mcr.microsoft.com/azurelinux/base/core:${AZURE_LINUX_TAG} AS snapshot
ARG CCF_VERSION
RUN tdnf install -y ca-certificates jq && \
    curl -L "https://github.com/microsoft/CCF/releases/download/ccf-${CCF_VERSION}/reproduce.json" \
      -o /tmp/reproduce.json && \
    jq -r '.tdnf_snapshottime' /tmp/reproduce.json > /tmp/tdnf_snapshottime

FROM mcr.microsoft.com/azurelinux/base/core:${AZURE_LINUX_TAG} AS builder
ARG CCF_VERSION
ARG SCITT_COMMIT
COPY --from=snapshot /tmp/tdnf_snapshottime /tmp/tdnf_snapshottime
RUN TDNF_SNAPSHOTTIME="$(cat /tmp/tdnf_snapshottime)" && \
    tdnf install -y --snapshottime="${TDNF_SNAPSHOTTIME}" ca-certificates git rpm-build && \
    tdnf install -y --snapshottime="${TDNF_SNAPSHOTTIME}" \
      "https://github.com/microsoft/CCF/releases/download/ccf-${CCF_VERSION}/ccf_devel_${CCF_VERSION//-/_}_x86_64.rpm"
COPY --from=source /src/app /tmp/app
RUN mkdir /tmp/app-build && \
    cd /tmp/app-build && \
    CC=clang-18 CXX=clang++-18 cmake -GNinja \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DCMAKE_INSTALL_PREFIX=/usr/src/app \
      -DBUILD_TESTS=OFF \
      -DSCITT_VERSION_OVERRIDE="0.18.1-${SCITT_COMMIT}" \
      /tmp/app && \
    ninja && \
    cpack -G RPM

FROM mcr.microsoft.com/azurelinux/base/core:${AZURE_LINUX_TAG}
ARG CCF_VERSION
COPY --from=snapshot /tmp/tdnf_snapshottime /tmp/tdnf_snapshottime
COPY --from=builder /tmp/app-build/*.rpm /tmp/
COPY --from=source /src/pyscitt /tmp/pyscitt

RUN TDNF_SNAPSHOTTIME="$(cat /tmp/tdnf_snapshottime)" && \
    tdnf install -y --snapshottime="${TDNF_SNAPSHOTTIME}" \
      ca-certificates curl nmap-ncat openssl python3-pip tini && \
    tdnf install -y --snapshottime="${TDNF_SNAPSHOTTIME}" /tmp/*.rpm && \
    mv /opt/scitt/bin/cchost /usr/bin/cchost && \
    python3 -m pip install --no-cache-dir "ccf==${CCF_VERSION}" /tmp/pyscitt && \
    rm -rf /tmp/*.rpm /tmp/pyscitt /var/cache/tdnf /var/lib/tdnf/*

WORKDIR /opt/master-thesis-transparency-log
COPY bootstrap.py ./bootstrap.py

ENV SCITT_DATA_DIR=/data \
    SCITT_HOST=0.0.0.0 \
    SCITT_PORT=8000 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
  CMD curl --fail --silent --insecure https://127.0.0.1:8000/node/network >/dev/null || exit 1

ENTRYPOINT ["tini", "--", "python3", "/opt/master-thesis-transparency-log/bootstrap.py"]
