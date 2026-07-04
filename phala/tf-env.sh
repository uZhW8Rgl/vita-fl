#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
SHARED_ENV_FILE="${ROOT_DIR}/.env.shared"
DEFAULT_ENV_FILE="${ROOT_DIR}/.env.anvil"
FALLBACK_ENV_FILE="${ROOT_DIR}/.env"
REPO_TERRAFORM_BIN="${SCRIPT_DIR}/bin/terraform"

if [ -n "${PHALA_ENV_FILE:-}" ]; then
  ENV_FILE="${PHALA_ENV_FILE}"
elif [ -f "${DEFAULT_ENV_FILE}" ]; then
  ENV_FILE="${DEFAULT_ENV_FILE}"
else
  ENV_FILE="${FALLBACK_ENV_FILE}"
fi

if [ ! -f "${ENV_FILE}" ]; then
  echo "Missing environment file: ${ENV_FILE}"
  exit 1
fi

ENV_FILES=()

if [ -f "${SHARED_ENV_FILE}" ]; then
  ENV_FILES+=("${SHARED_ENV_FILE}")
fi

ENV_FILES+=("${ENV_FILE}")

read_env_value() {
  local key="$1"
  local file
  local value=""

  for file in "${ENV_FILES[@]}"; do
    if [ ! -f "${file}" ]; then
      continue
    fi

    local match
    match=$(grep -E "^${key}=" "${file}" | tail -n 1 || true)
    if [ -n "${match}" ]; then
      value="${match#*=}"
    fi
  done

  printf '%s' "${value}"
}

require_env_value() {
  local key="$1"
  local value
  value=$(read_env_value "${key}")

  if [ -z "${value}" ]; then
    echo "${key} is required in ${ENV_FILES[*]}"
    exit 1
  fi

  printf '%s' "${value}"
}

PHALA_CLOUD_API_KEY=$(require_env_value "PHALA_CLOUD_API_KEY")
W0_ACCOUNT_ADDRESS=$(require_env_value "W0_ACCOUNT_ADDRESS")
W0_PRIVATE_KEY=$(require_env_value "W0_PRIVATE_KEY")

if [ -n "${TERRAFORM_BIN:-}" ]; then
  TERRAFORM_CMD="${TERRAFORM_BIN}"
elif command -v terraform >/dev/null 2>&1; then
  TERRAFORM_CMD="$(command -v terraform)"
elif [ -x "${REPO_TERRAFORM_BIN}" ]; then
  TERRAFORM_CMD="${REPO_TERRAFORM_BIN}"
else
  echo "Terraform binary not found. Install terraform or set TERRAFORM_BIN=/path/to/terraform."
  exit 1
fi

terraform_args=(
  -chdir="${SCRIPT_DIR}"
  "$@"
  -var="phala_cloud_api_key=${PHALA_CLOUD_API_KEY}"
  -var="account_address=${W0_ACCOUNT_ADDRESS}"
  -var="private_key=${W0_PRIVATE_KEY}"
)

append_var_if_set() {
  local tf_name="$1"
  local env_name="$2"
  local value
  value=$(read_env_value "${env_name}")

  if [ -n "${value}" ]; then
    terraform_args+=(-var="${tf_name}=${value}")
  fi
}

build_additional_workers_var() {
  local worker_count
  worker_count=$(read_env_value "WORKER_COUNT")
  worker_count="${worker_count:-1}"
  local map_entries=()
  local idx

  if ! [[ "${worker_count}" =~ ^[0-9]+$ ]]; then
    return
  fi

  for ((idx = 1; idx < worker_count; idx++)); do
    local account_var="W${idx}_ACCOUNT_ADDRESS"
    local key_var="W${idx}_PRIVATE_KEY"
    local account_value
    local key_value
    account_value=$(read_env_value "${account_var}")
    key_value=$(read_env_value "${key_var}")

    if [ -z "${account_value}" ] || [ -z "${key_value}" ]; then
      continue
    fi

    map_entries+=(
      "\"worker${idx}\"={app_name=\"master-thesis-dfl-worker-${idx}\",account_address=\"${account_value}\",private_key=\"${key_value}\"}"
    )
  done

  if [ "${#map_entries[@]}" -gt 0 ]; then
    local joined
    joined=$(IFS=,; echo "${map_entries[*]}")
    terraform_args+=(-var="additional_workers={${joined}}")
  fi
}

append_var_if_set "runtime_w1_account_address" "W1_ACCOUNT_ADDRESS"
append_var_if_set "initial_gm_signer_address" "INITIAL_GM_SIGNER_ADDRESS"
append_var_if_set "blockchain_provider" "BLOCKCHAIN_PROVIDER"
append_var_if_set "eth_wallet_private_key" "ETH_WALLET_PRIVATE_KEY"
append_var_if_set "initial_gm_cid" "INITIAL_GM_CID"
append_var_if_set "initial_gm_sig_cid" "INITIAL_GM_SIG_CID"
append_var_if_set "eth_eur_price" "ETH_EUR_PRICE"
append_var_if_set "transaction_cost_csv" "TRANSACTION_COST_CSV"
append_var_if_set "client_limit" "CLIENT_LIMIT"
append_var_if_set "epoch" "EPOCH"
append_var_if_set "round" "ROUND"
append_var_if_set "model_submission_deadline_ms" "MODEL_SUBMISSION_DEADLINE_MS"
append_var_if_set "gm_update_timeout_ms" "GM_UPDATE_TIMEOUT_MS"
append_var_if_set "gm_update_timeout_loops" "GM_UPDATE_TIMEOUT_LOOPS"
append_var_if_set "aggregation_update_estimate_ms" "AGGREGATION_UPDATE_ESTIMATE_MS"
append_var_if_set "gm_update_poll_ms" "GM_UPDATE_POLL_MS"
append_var_if_set "model_transfer_timeout_ms" "MODEL_TRANSFER_TIMEOUT_MS"
append_var_if_set "model_transfer_retry_delay_ms" "MODEL_TRANSFER_RETRY_DELAY_MS"
append_var_if_set "dataset_name" "DATASET_NAME"
append_var_if_set "pccs_fmspc" "PCCS_FMSPC"
append_var_if_set "pccs_fetch" "PCCS_FETCH"
append_var_if_set "pccs_tee" "PCCS_TEE"
append_var_if_set "p256_mode" "P256_MODE"
append_var_if_set "p256_verifier_address" "P256_VERIFIER_ADDRESS"
append_var_if_set "deploy_tdx_v4_dcap" "DEPLOY_TDX_V4_DCAP"
append_var_if_set "verify_tdx_quote_onchain" "VERIFY_TDX_QUOTE_ONCHAIN"
append_var_if_set "aggregator_timeout_report_percent" "AGGREGATOR_TIMEOUT_REPORT_PERCENT"
append_var_if_set "keep_alive" "KEEP_ALIVE"
append_var_if_set "pccs_quote_path" "PCCS_QUOTE_PATH"
append_var_if_set "tdx_quote_path" "TDX_QUOTE_PATH"
append_var_if_set "tdx_reference_quote_path" "TDX_REFERENCE_QUOTE_PATH"
append_var_if_set "phala_compose_path" "PHALA_COMPOSE_PATH"
append_var_if_set "phala_app_code_path" "PHALA_APP_CODE_PATH"
append_var_if_set "phala_rtmr3_event_log_path" "PHALA_RTMR3_EVENT_LOG_PATH"
append_var_if_set "runtime_endpoint_override" "PHALA_RUNTIME_ENDPOINT_OVERRIDE"

if [ -n "$(read_env_value "PUBLIC_IP")" ]; then
  terraform_args+=(-var="public_ip=$(read_env_value "PUBLIC_IP")")
fi

if [ -n "$(read_env_value "MSG_BROKER_IP")" ]; then
  terraform_args+=(-var="msg_broker_ip=$(read_env_value "MSG_BROKER_IP")")
fi

build_additional_workers_var

export PHALA_CLOUD_API_KEY

exec "${TERRAFORM_CMD}" "${terraform_args[@]}"
