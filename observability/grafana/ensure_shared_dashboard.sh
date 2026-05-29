#!/bin/sh
set -eu

GRAFANA_URL="${GRAFANA_URL:-http://grafana:3000}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-admin}"
DASHBOARD_UID="${DASHBOARD_UID:-dfl-training-overview}"
PUBLIC_DASHBOARD_UID="${PUBLIC_DASHBOARD_UID:-c656176f-25ca-4408-a109-731176af6da4}"
PUBLIC_DASHBOARD_TOKEN="${PUBLIC_DASHBOARD_TOKEN:-c656176f25ca4408a109731176af6da4}"

auth_args="-u ${GRAFANA_USER}:${GRAFANA_PASSWORD}"

echo "Waiting for Grafana at ${GRAFANA_URL}..."
for _ in $(seq 1 60); do
  if curl -fsS ${auth_args} "${GRAFANA_URL}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

dashboard_json="$(curl -fsS ${auth_args} "${GRAFANA_URL}/api/dashboards/uid/${DASHBOARD_UID}/public-dashboards/" || true)"

if [ -n "${dashboard_json}" ]; then
  existing_uid="$(printf '%s' "${dashboard_json}" | sed -n 's/.*"uid":"\([^"]*\)".*/\1/p')"
  if [ -n "${existing_uid}" ]; then
    echo "Shared dashboard already exists, enabling it again..."
    curl -fsS ${auth_args} \
      -X PATCH \
      -H "Content-Type: application/json" \
      -d '{"isEnabled":true,"timeSelectionEnabled":true,"annotationsEnabled":false,"share":"public"}' \
      "${GRAFANA_URL}/api/dashboards/uid/${DASHBOARD_UID}/public-dashboards/${existing_uid}" >/dev/null
    exit 0
  fi
fi

echo "Creating shared dashboard with fixed uid and token..."
create_payload="$(cat <<EOF
{"uid":"${PUBLIC_DASHBOARD_UID}","accessToken":"${PUBLIC_DASHBOARD_TOKEN}","timeSelectionEnabled":true,"isEnabled":true,"annotationsEnabled":false,"share":"public"}
EOF
)"

curl -fsS ${auth_args} \
  -X POST \
  -H "Content-Type: application/json" \
  -d "${create_payload}" \
  "${GRAFANA_URL}/api/dashboards/uid/${DASHBOARD_UID}/public-dashboards/" >/dev/null || {
    echo "Create failed, attempting to re-read and patch existing shared dashboard..."
    dashboard_json="$(curl -fsS ${auth_args} "${GRAFANA_URL}/api/dashboards/uid/${DASHBOARD_UID}/public-dashboards/")"
    existing_uid="$(printf '%s' "${dashboard_json}" | sed -n 's/.*"uid":"\([^"]*\)".*/\1/p')"
    if [ -z "${existing_uid}" ]; then
      echo "Could not determine shared dashboard uid from Grafana response." >&2
      exit 1
    fi
    curl -fsS ${auth_args} \
      -X PATCH \
      -H "Content-Type: application/json" \
      -d '{"isEnabled":true,"timeSelectionEnabled":true,"annotationsEnabled":false,"share":"public"}' \
      "${GRAFANA_URL}/api/dashboards/uid/${DASHBOARD_UID}/public-dashboards/${existing_uid}" >/dev/null
  }

echo "Shared dashboard ensured at ${GRAFANA_URL}/public-dashboards/${PUBLIC_DASHBOARD_TOKEN}"
