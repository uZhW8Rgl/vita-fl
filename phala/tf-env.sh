#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
SHARED_ENV_FILE="${ROOT_DIR}/.env.shared"
DEFAULT_ENV_FILE="${ROOT_DIR}/.env.phala.anvil"
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

resolve_required_file() {
  local configured_path="$1"
  local description="$2"
  local resolved_path="${configured_path}"

  if [[ "${resolved_path}" != /* ]]; then
    resolved_path="${ROOT_DIR}/${resolved_path}"
  fi
  if [ ! -r "${resolved_path}" ]; then
    echo "Missing ${description}: ${resolved_path}" >&2
    echo "Generate local demo keys with scripts/prepare_dfl_worker_experiment.py or configure an explicit file path." >&2
    exit 1
  fi

  printf '%s/%s' "$(cd "$(dirname "${resolved_path}")" && pwd)" "$(basename "${resolved_path}")"
}

W0_RSA_PRIVATE_KEY_FILE=$(read_env_value "W0_RSA_PRIVATE_KEY_FILE")
W0_RSA_PUBLIC_KEY_FILE=$(read_env_value "W0_RSA_PUBLIC_KEY_FILE")
W0_RSA_PRIVATE_KEY_FILE=${W0_RSA_PRIVATE_KEY_FILE:-data/rsa_keys/private_key.pem}
W0_RSA_PUBLIC_KEY_FILE=${W0_RSA_PUBLIC_KEY_FILE:-data/rsa_keys/public_key.pem}
W0_RSA_PRIVATE_KEY_PATH=$(resolve_required_file "${W0_RSA_PRIVATE_KEY_FILE}" "worker-0 RSA private key")
W0_RSA_PUBLIC_KEY_PATH=$(resolve_required_file "${W0_RSA_PUBLIC_KEY_FILE}" "worker-0 RSA public key")

INITIAL_GM_SIGNING_KEY_FILE=$(read_env_value "INITIAL_GM_SIGNING_KEY_FILE")
INITIAL_GM_SIGNING_KEY_FILE=${INITIAL_GM_SIGNING_KEY_FILE:-${W0_RSA_PRIVATE_KEY_FILE}}
INITIAL_GM_SIGNING_KEY_PATH=$(resolve_required_file "${INITIAL_GM_SIGNING_KEY_FILE}" "initial GM signing key")

ETH_WALLET_PRIVATE_KEY=$(read_env_value "ETH_WALLET_PRIVATE_KEY")
ETH_WALLET_PRIVATE_KEY=${ETH_WALLET_PRIVATE_KEY:-${W0_PRIVATE_KEY}}

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
)

export TF_VAR_phala_cloud_api_key="${PHALA_CLOUD_API_KEY}"
export TF_VAR_account_address="${W0_ACCOUNT_ADDRESS}"
export TF_VAR_private_key="${W0_PRIVATE_KEY}"
export TF_VAR_eth_wallet_private_key="${ETH_WALLET_PRIVATE_KEY}"
export TF_VAR_rsa_private_key_path="${W0_RSA_PRIVATE_KEY_PATH}"
export TF_VAR_rsa_public_key_path="${W0_RSA_PUBLIC_KEY_PATH}"
export TF_VAR_initial_gm_signing_key_path="${INITIAL_GM_SIGNING_KEY_PATH}"

append_var_if_set() {
  local tf_name="$1"
  local env_name="$2"
  local value
  value=$(read_env_value "${env_name}")

  if [ -n "${value}" ]; then
    printf -v "TF_VAR_${tf_name}" '%s' "${value}"
    export "TF_VAR_${tf_name}"
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
    local key_suffix
    local rsa_private_file
    local rsa_public_file
    local rsa_private_path
    local rsa_public_path
    account_value=$(read_env_value "${account_var}")
    key_value=$(read_env_value "${key_var}")

    if [ -z "${account_value}" ] || [ -z "${key_value}" ]; then
      continue
    fi

    key_suffix="_${idx}"
    rsa_private_file=$(read_env_value "W${idx}_RSA_PRIVATE_KEY_FILE")
    rsa_public_file=$(read_env_value "W${idx}_RSA_PUBLIC_KEY_FILE")
    rsa_private_file=${rsa_private_file:-data/rsa_keys/private_key${key_suffix}.pem}
    rsa_public_file=${rsa_public_file:-data/rsa_keys/public_key${key_suffix}.pem}
    rsa_private_path=$(resolve_required_file "${rsa_private_file}" "worker-${idx} RSA private key")
    rsa_public_path=$(resolve_required_file "${rsa_public_file}" "worker-${idx} RSA public key")

    map_entries+=(
      "\"worker${idx}\"={app_name=\"master-thesis-dfl-worker-${idx}\",account_address=\"${account_value}\",private_key=\"${key_value}\",rsa_private_key_path=\"${rsa_private_path}\",rsa_public_key_path=\"${rsa_public_path}\"}"
    )
  done

  if [ "${#map_entries[@]}" -gt 0 ]; then
    local joined
    joined=$(IFS=,; echo "${map_entries[*]}")
    export TF_VAR_additional_workers="{${joined}}"
  fi
}

append_var_if_set "runtime_w1_account_address" "W1_ACCOUNT_ADDRESS"
append_var_if_set "initial_gm_signer_address" "INITIAL_GM_SIGNER_ADDRESS"
append_var_if_set "blockchain_provider" "BLOCKCHAIN_PROVIDER"
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
append_var_if_set "train_images_src" "TRAIN_IMAGES_SRC"
append_var_if_set "train_labels_src" "TRAIN_LABELS_SRC"
append_var_if_set "test_images_src" "TEST_IMAGES_SRC"
append_var_if_set "test_labels_src" "TEST_LABELS_SRC"
append_var_if_set "train_data_src" "TRAIN_DATA_SRC"
append_var_if_set "test_data_src" "TEST_DATA_SRC"
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
append_var_if_set "phala_rtmr3_event_digests" "PHALA_RTMR3_EVENT_DIGESTS"
append_var_if_set "phala_enforce_compose_hash" "PHALA_ENFORCE_COMPOSE_HASH"
append_var_if_set "runtime_endpoint_override" "PHALA_RUNTIME_ENDPOINT_OVERRIDE"
append_var_if_set "runtime_rpc_url_override" "PHALA_RUNTIME_RPC_URL"
append_var_if_set "runtime_kubo_api_url_override" "PHALA_RUNTIME_KUBO_API_URL"
append_var_if_set "runtime_kubo_gateway_url_override" "PHALA_RUNTIME_KUBO_GATEWAY_URL"
append_var_if_set "phala_allowed_worker_compose_hashes" "PHALA_ALLOWED_WORKER_COMPOSE_HASHES"

append_var_if_set "public_ip" "PUBLIC_IP"
append_var_if_set "msg_broker_ip" "MSG_BROKER_IP"

build_additional_workers_var

export PHALA_CLOUD_API_KEY

exec "${TERRAFORM_CMD}" "${terraform_args[@]}"
