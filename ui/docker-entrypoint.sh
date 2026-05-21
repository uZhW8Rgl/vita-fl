#!/bin/sh
set -eu

: "${GRAFANA_EXTERNAL_DASHBOARD_URL:=}"

escaped_url=$(printf '%s' "$GRAFANA_EXTERNAL_DASHBOARD_URL" | sed 's/[\/&]/\\&/g')
sed -i "s|__GRAFANA_EXTERNAL_DASHBOARD_URL__|$escaped_url|g" /usr/share/nginx/html/index.html
