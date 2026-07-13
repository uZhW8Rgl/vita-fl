#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
ENV_FILE=${PHALA_ENV_FILE:-${ROOT_DIR}/.env.phala.anvil}
TERRAFORM_BIN=${TERRAFORM_BIN:-${SCRIPT_DIR}/bin/terraform}

if [ ! -f "${ENV_FILE}" ]; then
  echo "Missing ${ENV_FILE}. Copy .env.phala.anvil.example and replace its placeholders." >&2
  exit 1
fi

read_value() {
  local key=$1
  sed -n "s/^${key}=//p" "${ENV_FILE}" | tail -n 1
}

derive_url() {
  local endpoint=$1
  local port=$2
  if [[ "${endpoint}" =~ -[0-9]+\. ]]; then
    printf '%s' "${endpoint}" | sed -E "s/-[0-9]+\\./-${port}./"
  else
    printf '%s:%s' "${endpoint%/}" "${port}"
  fi
}

PHALA_ENV_FILE="${ENV_FILE}" bash "${SCRIPT_DIR}/tf-env.sh" init -input=false

endpoint=$(read_value PHALA_RUNTIME_ENDPOINT_OVERRIDE)
if [ -z "${endpoint}" ]; then
  endpoint=$("${TERRAFORM_BIN}" -chdir="${SCRIPT_DIR}" output -raw contracts_endpoint 2>/dev/null || true)
fi

if [ -z "${endpoint}" ]; then
  PHALA_ENV_FILE="${ENV_FILE}" bash "${SCRIPT_DIR}/tf-env.sh" apply -input=false -auto-approve
  endpoint=$("${TERRAFORM_BIN}" -chdir="${SCRIPT_DIR}" output -raw contracts_endpoint)
fi

endpoint=${endpoint%/}
export TF_VAR_runtime_endpoint_override="${endpoint}"
export TF_VAR_runtime_rpc_url_override
TF_VAR_runtime_rpc_url_override=$(derive_url "${endpoint}" 8545)
export TF_VAR_runtime_kubo_api_url_override
TF_VAR_runtime_kubo_api_url_override=$(derive_url "${endpoint}" 5001)
export TF_VAR_runtime_kubo_gateway_url_override
TF_VAR_runtime_kubo_gateway_url_override=$(derive_url "${endpoint}" 8080)

PHALA_ENV_FILE="${ENV_FILE}" bash "${SCRIPT_DIR}/tf-env.sh" apply -input=false -auto-approve
