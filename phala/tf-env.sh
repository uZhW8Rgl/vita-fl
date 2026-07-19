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

resolve_key_file_or_inline() {
  local configured_path="$1"
  local description="$2"
  local env_name="$3"
  local output_name="$4"
  local mode="$5"
  local resolved_path="${configured_path}"

  if [[ "${resolved_path}" != /* ]]; then
    resolved_path="${ROOT_DIR}/${resolved_path}"
  fi
  if [ -r "${resolved_path}" ]; then
    printf '%s' "${resolved_path}"
    return
  fi

  local inline_value
  inline_value=$(read_env_value "${env_name}")
  if [ -z "${inline_value}" ]; then
    echo "Missing ${description}: ${resolved_path}, and ${env_name} is empty" >&2
    exit 1
  fi

  local output_dir="${TMPDIR:-/tmp}/master-thesis-phala-keys"
  local output_path="${output_dir}/${output_name}"
  mkdir -p "${output_dir}"
  inline_value=${inline_value//\\n/$'\n'}
  (umask 077; printf '%s\n' "${inline_value}" >"${output_path}")
  chmod "${mode}" "${output_path}"
  printf '%s' "${output_path}"
}

W0_RSA_PRIVATE_KEY_FILE=$(read_env_value "W0_RSA_PRIVATE_KEY_FILE")
W0_RSA_PUBLIC_KEY_FILE=$(read_env_value "W0_RSA_PUBLIC_KEY_FILE")
W0_RSA_PRIVATE_KEY_FILE=${W0_RSA_PRIVATE_KEY_FILE:-data/rsa_keys/private_key.pem}
W0_RSA_PUBLIC_KEY_FILE=${W0_RSA_PUBLIC_KEY_FILE:-data/rsa_keys/public_key.pem}
W0_RSA_PRIVATE_KEY_PATH=$(resolve_key_file_or_inline "${W0_RSA_PRIVATE_KEY_FILE}" "worker-0 RSA private key" "W0_RSA_PRIVATE_KEY" "w0-private.pem" 600)
W0_RSA_PUBLIC_KEY_PATH=$(resolve_key_file_or_inline "${W0_RSA_PUBLIC_KEY_FILE}" "worker-0 RSA public key" "W0_RSA_PUBLIC_KEY" "w0-public.pem" 644)

INITIAL_GM_SIGNING_KEY_FILE=$(read_env_value "INITIAL_GM_SIGNING_KEY_FILE")
INITIAL_GM_SIGNING_KEY_FILE=${INITIAL_GM_SIGNING_KEY_FILE:-${W0_RSA_PRIVATE_KEY_FILE}}
INITIAL_GM_SIGNING_KEY_PATH=$(resolve_key_file_or_inline "${INITIAL_GM_SIGNING_KEY_FILE}" "initial GM signing key" "W0_RSA_PRIVATE_KEY" "initial-gm-signing-key.pem" 600)

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
  local dynamic_control
  dynamic_control=$(read_env_value "ENABLE_PHALA_CONTROL_API")
  if [ "${dynamic_control}" = "1" ] || [ "${dynamic_control}" = "true" ]; then
    export TF_VAR_additional_workers="{}"
    return
  fi

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

build_dynamic_worker_inventory() {
  local maximum
  maximum=$(read_env_value "MAX_DYNAMIC_WORKERS")
  maximum="${maximum:-20}"
  if ! [[ "${maximum}" =~ ^[0-9]+$ ]] || [ "${maximum}" -lt 1 ] || [ "${maximum}" -gt 20 ]; then
    echo "MAX_DYNAMIC_WORKERS must be an integer between 1 and 20" >&2
    exit 1
  fi

  local command=(python3 "${SCRIPT_DIR}/build_worker_inventory.py" --max-workers "${maximum}")
  local file
  for file in "${ENV_FILES[@]}"; do
    command+=(--env-file "${file}")
  done
  export TF_VAR_dynamic_worker_inventory
  TF_VAR_dynamic_worker_inventory=$("${command[@]}")
  export TF_VAR_max_dynamic_workers="${maximum}"
}

configure_phala_control_api() {
  local enabled
  enabled=$(read_env_value "ENABLE_PHALA_CONTROL_API")
  enabled="${enabled:-false}"
  export TF_VAR_enable_phala_control_api="${enabled}"

  if [ "${enabled}" != "1" ] && [ "${enabled}" != "true" ]; then
    return
  fi

  export TF_VAR_control_admin_token
  TF_VAR_control_admin_token=$(require_env_value "CONTROL_ADMIN_TOKEN")
  append_var_if_set "control_api_image" "CONTROL_API_IMAGE"

}

configure_phala_ui() {
  local enabled
  enabled=$(read_env_value "ENABLE_PHALA_UI")
  enabled="${enabled:-false}"
  export TF_VAR_enable_phala_ui="${enabled}"

  if [ "${enabled}" != "1" ] && [ "${enabled}" != "true" ]; then
    return
  fi

  export TF_VAR_ui_basic_auth_username
  TF_VAR_ui_basic_auth_username=${UI_BASIC_AUTH_USERNAME:-$(read_env_value "UI_BASIC_AUTH_USERNAME")}
  export TF_VAR_ui_basic_auth_password
  TF_VAR_ui_basic_auth_password=${UI_BASIC_AUTH_PASSWORD:-$(read_env_value "UI_BASIC_AUTH_PASSWORD")}
  if [ -z "${TF_VAR_ui_basic_auth_username}" ] || [ -z "${TF_VAR_ui_basic_auth_password}" ]; then
    echo "UI_BASIC_AUTH_USERNAME and UI_BASIC_AUTH_PASSWORD are required when ENABLE_PHALA_UI=true" >&2
    exit 1
  fi
  append_var_if_set "ui_image" "UI_IMAGE"
}

configure_phala_agent() {
  local enabled
  enabled=$(read_env_value "ENABLE_PHALA_AGENT")
  enabled="${enabled:-false}"
  export TF_VAR_enable_phala_agent="${enabled}"

  local ollama_enabled
  ollama_enabled=$(read_env_value "ENABLE_OLLAMA")
  ollama_enabled="${ollama_enabled:-false}"
  export TF_VAR_enable_ollama="${ollama_enabled}"

  append_var_if_set "agent_image" "AGENT_IMAGE"
  append_var_if_set "transparency_log_image" "TRANSPARENCY_LOG_IMAGE"
  append_var_if_set "ollama_image" "OLLAMA_IMAGE"
  append_var_if_set "ollama_model" "OLLAMA_MODEL"
  append_var_if_set "ollama_size" "OLLAMA_SIZE"
  append_var_if_set "ollama_base_url_override" "OLLAMA_BASE_URL_OVERRIDE"
  append_var_if_set "tee_inference_url_override" "TEE_INFERENCE_URL_OVERRIDE"
  append_var_if_set "zk_inference_url_override" "ZK_INFERENCE_URL_OVERRIDE"
  local sello_enabled
  sello_enabled=$(read_env_value "ENABLE_SELLO_RECEIPTS")
  sello_enabled="${sello_enabled:-false}"
  export TF_VAR_enable_sello_receipts="${sello_enabled}"
  if [ "${sello_enabled}" = "1" ] || [ "${sello_enabled}" = "true" ]; then
    export TF_VAR_sello_token_issuer_signing_seed
    TF_VAR_sello_token_issuer_signing_seed=$(require_env_value "SELLO_TOKEN_ISSUER_SIGNING_SEED")
    export TF_VAR_sello_owner_hpke_private_key
    TF_VAR_sello_owner_hpke_private_key=$(require_env_value "SELLO_OWNER_HPKE_PRIVATE_KEY")
    export TF_VAR_sello_token_issuer_public_key
    TF_VAR_sello_token_issuer_public_key=$(require_env_value "SELLO_TOKEN_ISSUER_PUBLIC_KEY")
    export TF_VAR_sello_tee_service_signing_seed
    TF_VAR_sello_tee_service_signing_seed=$(require_env_value "SELLO_TEE_SERVICE_SIGNING_SEED")
    export TF_VAR_sello_zk_service_signing_seed
    TF_VAR_sello_zk_service_signing_seed=$(require_env_value "SELLO_ZK_SERVICE_SIGNING_SEED")
    export TF_VAR_sello_service_registry
    TF_VAR_sello_service_registry=$(require_env_value "SELLO_SERVICE_REGISTRY")
    export TF_VAR_sello_scitt_url
    TF_VAR_sello_scitt_url=$(require_env_value "SELLO_SCITT_URL")
  fi
  if [ "${ollama_enabled}" = "1" ] || [ "${ollama_enabled}" = "true" ]; then
    export TF_VAR_ollama_api_token
    TF_VAR_ollama_api_token=$(require_env_value "OLLAMA_API_TOKEN")
  fi
}

derive_runtime_service_urls() {
  local endpoint
  endpoint=$(read_env_value "PHALA_RUNTIME_ENDPOINT_OVERRIDE")
  if [ -z "${endpoint}" ]; then
    return
  fi
  endpoint="${endpoint%/}"

  derive_url() {
    local port="$1"
    if [[ "${endpoint}" =~ -[0-9]+\. ]]; then
      printf '%s' "${endpoint}" | sed -E "s/-[0-9]+\\./-${port}./"
    else
      printf '%s:%s' "${endpoint}" "${port}"
    fi
  }

  if [ -z "${TF_VAR_runtime_rpc_url_override:-}" ]; then
    export TF_VAR_runtime_rpc_url_override
    TF_VAR_runtime_rpc_url_override=$(derive_url 8545)
  fi
  if [ -z "${TF_VAR_runtime_kubo_api_url_override:-}" ]; then
    export TF_VAR_runtime_kubo_api_url_override
    TF_VAR_runtime_kubo_api_url_override=$(derive_url 5001)
  fi
  if [ -z "${TF_VAR_runtime_kubo_gateway_url_override:-}" ]; then
    export TF_VAR_runtime_kubo_gateway_url_override
    TF_VAR_runtime_kubo_gateway_url_override=$(derive_url 8080)
  fi
}

append_var_if_set "runtime_w1_account_address" "W1_ACCOUNT_ADDRESS"
append_var_if_set "worker_image" "WORKER_IMAGE"
append_var_if_set "smart_contracts_image" "SMART_CONTRACTS_IMAGE"
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
append_var_if_set "enable_tee_inference" "ENABLE_TEE_INFERENCE"
append_var_if_set "tee_inference_image" "TEE_INFERENCE_IMAGE"
append_var_if_set "enable_zk_inference" "ENABLE_ZK_INFERENCE"
append_var_if_set "zk_inference_image" "ZK_INFERENCE_IMAGE"
append_var_if_set "manage_power_state" "MANAGE_POWER_STATE"
append_var_if_set "desired_power_state" "DESIRED_POWER_STATE"
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
append_var_if_set "runtime_endpoint_override" "PHALA_RUNTIME_ENDPOINT_OVERRIDE"
append_var_if_set "runtime_rpc_url_override" "PHALA_RUNTIME_RPC_URL"
append_var_if_set "runtime_kubo_api_url_override" "PHALA_RUNTIME_KUBO_API_URL"
append_var_if_set "runtime_kubo_gateway_url_override" "PHALA_RUNTIME_KUBO_GATEWAY_URL"
append_var_if_set "public_ip" "PUBLIC_IP"
append_var_if_set "msg_broker_ip" "MSG_BROKER_IP"

build_additional_workers_var
build_dynamic_worker_inventory
derive_runtime_service_urls
configure_phala_control_api
configure_phala_ui
configure_phala_agent

export PHALA_CLOUD_API_KEY

exec "${TERRAFORM_CMD}" "${terraform_args[@]}"
