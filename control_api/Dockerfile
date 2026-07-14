FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        docker-cli \
        docker.io \
        docker-compose && \
    command -v docker && \
    command -v docker-compose && \
    rm -rf /var/lib/apt/lists/*

COPY control_api ./control_api
COPY observability /opt/master-thesis-observability
COPY phala/bin/terraform /usr/local/bin/terraform
COPY phala/dynamic-workers ./phala/dynamic-workers
COPY control_api/terraform.rc /etc/terraform.rc

RUN chmod 0755 /usr/local/bin/terraform

ENV TF_CLI_CONFIG_FILE=/etc/terraform.rc

RUN terraform -chdir=/app/phala/dynamic-workers providers mirror /opt/terraform-provider-mirror

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install -r control_api/requirements.txt

CMD ["python", "control_api/server.py"]
