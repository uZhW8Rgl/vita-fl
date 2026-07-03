#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)

if [ ! -f "${ROOT_DIR}/.env" ]; then
  echo "Missing ${ROOT_DIR}/.env"
  exit 1
fi

set -a
. "${ROOT_DIR}/.env"
set +a

: "${PHALA_CLOUD_API_KEY:?PHALA_CLOUD_API_KEY is required in .env}"
: "${W0_ACCOUNT_ADDRESS:?W0_ACCOUNT_ADDRESS is required in .env}"
: "${W0_PRIVATE_KEY:?W0_PRIVATE_KEY is required in .env}"

exec /tmp/terraform-bin/terraform -chdir="${SCRIPT_DIR}" "$@" \
  -var="account_address=${W0_ACCOUNT_ADDRESS}" \
  -var="private_key=${W0_PRIVATE_KEY}"
