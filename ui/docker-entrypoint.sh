#!/bin/sh
set -eu

: "${GRAFANA_EXTERNAL_DASHBOARD_URL:=}"
: "${GRAFANA_CONTRACT_DASHBOARD_URL:=}"
: "${GRAFANA_TRAINING_DASHBOARD_URL:=}"
: "${GRAFANA_AGENT_DASHBOARD_URL:=}"
: "${CONTROL_ADMIN_TOKEN:=}"
: "${UI_BASIC_AUTH_USERNAME:?UI_BASIC_AUTH_USERNAME is required}"
: "${UI_BASIC_AUTH_PASSWORD:?UI_BASIC_AUTH_PASSWORD is required}"

htpasswd -bc /etc/nginx/.htpasswd "$UI_BASIC_AUTH_USERNAME" "$UI_BASIC_AUTH_PASSWORD" >/dev/null
chmod 0644 /etc/nginx/.htpasswd

escaped_url=$(printf '%s' "$GRAFANA_EXTERNAL_DASHBOARD_URL" | sed 's/[\/&]/\\&/g')
escaped_contract_url=$(printf '%s' "$GRAFANA_CONTRACT_DASHBOARD_URL" | sed 's/[\/&]/\\&/g')
escaped_training_url=$(printf '%s' "$GRAFANA_TRAINING_DASHBOARD_URL" | sed 's/[\/&]/\\&/g')
escaped_agent_url=$(printf '%s' "$GRAFANA_AGENT_DASHBOARD_URL" | sed 's/[\/&]/\\&/g')
escaped_control_token=$(printf '%s' "$CONTROL_ADMIN_TOKEN" | sed 's/[\/&]/\\&/g')
sed -i "s|__GRAFANA_EXTERNAL_DASHBOARD_URL__|$escaped_url|g" /usr/share/nginx/html/index.html
sed -i "s|__GRAFANA_CONTRACT_DASHBOARD_URL__|$escaped_contract_url|g" /usr/share/nginx/html/index.html
sed -i "s|__GRAFANA_TRAINING_DASHBOARD_URL__|$escaped_training_url|g" /usr/share/nginx/html/index.html
sed -i "s|__GRAFANA_AGENT_DASHBOARD_URL__|$escaped_agent_url|g" /usr/share/nginx/html/index.html
sed -i "s|__CONTROL_ADMIN_TOKEN__|$escaped_control_token|g" /etc/nginx/conf.d/default.conf
