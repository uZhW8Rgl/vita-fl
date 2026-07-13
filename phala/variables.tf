variable "phala_cloud_api_key" {
  description = "Optional explicit API key. Prefer the PHALA_CLOUD_API_KEY environment variable."
  type        = string
  sensitive   = true
  default     = null
}

variable "contracts_app_name" {
  description = "Name of the Phala Cloud app that runs anvil, ipfs, and smart-contract initialization."
  type        = string
  default     = "master-thesis-contract-runtime-phala"
}

variable "worker_app_name" {
  description = "Name of the Phala Cloud app that runs the DFL worker."
  type        = string
  default     = "master-thesis-dfl-worker-0"
}

variable "enable_tee_inference" {
  description = "Deploy the separate attested TEE inference Phala app using W0's authorized participant credentials."
  type        = bool
  default     = false
}

variable "tee_inference_app_name" {
  description = "Name of the Phala Cloud app that runs attested ChestMNIST inference."
  type        = string
  default     = "master-thesis-tee-inference"
}

variable "additional_workers" {
  description = "Additional DFL worker apps with independent account/key pairs."
  type = map(object({
    app_name             = string
    account_address      = string
    private_key          = string
    rsa_private_key_path = string
    rsa_public_key_path  = string
  }))
  sensitive = true
  default   = {}
}

variable "dynamic_worker_inventory" {
  description = "Encrypted JSON inventory containing the existing fixed W0-W19 worker identities."
  type        = string
  sensitive   = true
  default     = ""
}

variable "max_dynamic_workers" {
  description = "Maximum number of fixed worker identities exposed to the Phala control API."
  type        = number
  default     = 20

  validation {
    condition     = var.max_dynamic_workers >= 1 && var.max_dynamic_workers <= 20
    error_message = "max_dynamic_workers must be between 1 and 20."
  }
}

variable "enable_phala_control_api" {
  description = "Run the dynamic-worker Control API in the contract-runtime CVM."
  type        = bool
  default     = false
}

variable "control_api_image" {
  description = "Digest-pinned Control API image containing the dynamic worker Terraform module."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-control-api@sha256:bc69269b34b12c2c0dc150c5bb5b692a017d9c4b22c004623b92a7280495ba22"

  validation {
    condition = (
      !var.enable_phala_control_api ||
      can(regex("^ghcr\\.io/.+@sha256:[0-9a-f]{64}$", var.control_api_image))
    )
    error_message = "control_api_image must be digest-pinned when enable_phala_control_api is true."
  }
}

variable "control_admin_token" {
  description = "Bearer/header token used by the UI proxy to authorize worker lifecycle changes."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition = (
      !var.enable_phala_control_api ||
      can(regex("^[A-Za-z0-9_-]{16,128}$", var.control_admin_token))
    )
    error_message = "control_admin_token must contain 16-128 URL-safe characters when the Phala Control API is enabled."
  }
}

variable "enable_phala_ui" {
  description = "Run the browser UI and its authenticated Control API proxy in the contract-runtime CVM."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_phala_ui || var.enable_phala_control_api
    error_message = "enable_phala_ui requires enable_phala_control_api."
  }
}

variable "ui_image" {
  description = "Digest-pinned browser UI image."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-ui@sha256:f24dad0f181ec539166c64d65e37ed0d7ebf317f4d3c9cbb3d9f5ef91398d8b1"

  validation {
    condition = (
      !var.enable_phala_ui ||
      can(regex("^ghcr\\.io/.+@sha256:[0-9a-f]{64}$", var.ui_image))
    )
    error_message = "ui_image must be digest-pinned when enable_phala_ui is true."
  }
}

variable "ui_basic_auth_username" {
  description = "HTTP Basic Authentication username protecting the Phala UI and proxied Grafana dashboards."
  type        = string
  default     = ""

  validation {
    condition = (
      !var.enable_phala_ui ||
      can(regex("^[A-Za-z0-9._-]{3,64}$", var.ui_basic_auth_username))
    )
    error_message = "ui_basic_auth_username must contain 3-64 safe username characters when the Phala UI is enabled."
  }
}

variable "ui_basic_auth_password" {
  description = "HTTP Basic Authentication password protecting the Phala UI and proxied Grafana dashboards."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = !var.enable_phala_ui || length(var.ui_basic_auth_password) >= 16
    error_message = "ui_basic_auth_password must contain at least 16 characters when the Phala UI is enabled."
  }
}

variable "contracts_size" {
  description = "Phala CVM size slug for the contract-runtime TEE."
  type        = string
  default     = "tdx.small"
}

variable "worker_size" {
  description = "Phala CVM size slug for the worker TEE."
  type        = string
  default     = "tdx.small"
}

variable "tee_inference_size" {
  description = "Phala CVM size slug for the TEE inference app."
  type        = string
  default     = "tdx.small"
}

variable "region" {
  description = "Phala region slug."
  type        = string
  default     = "US-WEST-1"
}

variable "os_image" {
  description = "Phala OS image slug for the CVM."
  type        = string
  default     = "dstack-dev-0.5.7"
}

variable "contracts_disk_size" {
  description = "Disk size in GB for the contract-runtime TEE."
  type        = number
  default     = 20
}

variable "worker_disk_size" {
  description = "Disk size in GB for the worker TEE."
  type        = number
  default     = 20
}

variable "tee_inference_disk_size" {
  description = "Disk size in GB for the TEE inference app."
  type        = number
  default     = 20
}

variable "worker_replicas" {
  description = "Number of worker CVM replicas."
  type        = number
  default     = 1
}

variable "kms" {
  description = "KMS mode for provisioning."
  type        = string
  default     = "phala"
}

variable "listed" {
  description = "Whether the app should be publicly listed."
  type        = bool
  default     = false
}

variable "node_id" {
  description = "Optional worker node pinning target."
  type        = number
  default     = null
}

variable "custom_app_id" {
  description = "Optional deterministic custom app ID."
  type        = string
  default     = null
}

variable "nonce" {
  description = "Optional nonce paired with custom_app_id."
  type        = number
  default     = null
}

variable "storage_fs" {
  description = "Optional storage filesystem."
  type        = string
  default     = "zfs"
}

variable "pre_launch_script" {
  description = "Optional shell script executed before the CVM starts."
  type        = string
  default     = null
}

variable "public_logs" {
  description = "Expose container logs publicly."
  type        = bool
  default     = true
}

variable "public_sysinfo" {
  description = "Expose system information publicly."
  type        = bool
  default     = true
}

variable "public_tcbinfo" {
  description = "Expose TCB attestation information publicly."
  type        = bool
  default     = true
}

variable "gateway_enabled" {
  description = "Deprecated shared gateway flag. Prefer contracts_gateway_enabled / worker_gateway_enabled."
  type        = bool
  default     = false
}

variable "contracts_gateway_enabled" {
  description = "Enable the public gateway endpoint for the contract-runtime app."
  type        = bool
  default     = true
}

variable "worker_gateway_enabled" {
  description = "Enable the public gateway endpoint for the worker app."
  type        = bool
  default     = true
}

variable "tee_inference_gateway_enabled" {
  description = "Enable the public gateway endpoint for the TEE inference app."
  type        = bool
  default     = true
}

variable "secure_time" {
  description = "Enable secure time mode."
  type        = bool
  default     = false
}

variable "wait_for_ready" {
  description = "Wait until the app reports running replicas."
  type        = bool
  default     = true
}

variable "wait_timeout_seconds" {
  description = "Timeout for readiness and power-state waits."
  type        = number
  default     = 900
}

variable "manage_power_state" {
  description = "Whether Terraform should explicitly manage CVM power state after deployment."
  type        = bool
  default     = false
}

variable "desired_power_state" {
  description = "Desired CVM power state when manage_power_state is enabled."
  type        = string
  default     = "running"

  validation {
    condition     = contains(["running", "stopped"], var.desired_power_state)
    error_message = "desired_power_state must be either \"running\" or \"stopped\"."
  }
}

variable "manage_account_ssh_key" {
  description = "Whether to also register the SSH key at the Phala account level."
  type        = bool
  default     = false
}

variable "account_ssh_key_name" {
  description = "Display name for the optional account-level SSH key."
  type        = string
  default     = "master-thesis-operator"
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key that should be injected into the CVM."
  type        = string
  default     = null
}

variable "worker_image" {
  description = "Digest-pinned DFL worker container image."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:6ea849fd7c494829e258dea99a4aba7aa4095996cdb67f66b4ccf47d2120d0e3"
}

variable "smart_contracts_image" {
  description = "Container image for the smart-contract initialization service."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:70b65f1c30b5bdf8d50dd3ce01800e5615e5900d500a06be2dbbfdedb606d202"
}

variable "tee_inference_image" {
  description = "Digest-pinned TEE inference container image."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-tee-inference@sha256:cf78b6b5515f0d8d61ecb22612088db9dccf43dcd9311783239a30c90dd4054a"

  validation {
    condition     = can(regex("^ghcr\\.io/.+@sha256:[0-9a-f]{64}$", var.tee_inference_image))
    error_message = "tee_inference_image must be a digest-pinned ghcr.io reference."
  }
}

variable "anvil_image" {
  description = "Container image for the embedded Anvil service."
  type        = string
  default     = "ghcr.io/foundry-rs/foundry:nightly-dd0a687aa36ec731b6ea2cda2aa4f32e192814fa"
}

variable "ipfs_image" {
  description = "Container image for the embedded Kubo service."
  type        = string
  default     = "ipfs/kubo:latest"
}

variable "account_address" {
  description = "Worker account address."
  type        = string
}

variable "private_key" {
  description = "Worker private key."
  type        = string
  sensitive   = true
}

variable "runtime_w1_account_address" {
  description = "Optional second worker address injected into the contract-runtime compose."
  type        = string
  default     = null
}

variable "initial_gm_signer_address" {
  description = "Optional initial GM signer address injected into the contract-runtime compose."
  type        = string
  default     = null
}

variable "blockchain_provider" {
  description = "Blockchain provider selector injected into the contract-runtime compose."
  type        = string
  default     = "anvil"
}

variable "eth_wallet_private_key" {
  description = "Wallet private key used by the contract-runtime initialization container."
  type        = string
  sensitive   = true
  default     = ""
}

variable "initial_gm_cid" {
  description = "Initial global model CID injected into the contract-runtime compose."
  type        = string
  default     = ""
}

variable "initial_gm_sig_cid" {
  description = "Initial global model signature CID injected into the contract-runtime compose."
  type        = string
  default     = ""
}

variable "eth_eur_price" {
  description = "ETH/EUR conversion value used when exporting transaction costs."
  type        = string
  default     = "3000"
}

variable "transaction_cost_csv" {
  description = "Transaction-cost CSV path used by the contract-runtime initialization container."
  type        = string
  default     = "/dfl/data/evaluation/transaction_costs.csv"
}

variable "model_transfer_timeout_ms" {
  description = "Model transfer timeout in milliseconds."
  type        = string
  default     = "20000"
}

variable "model_transfer_retry_delay_ms" {
  description = "Delay between model transfer retries in milliseconds."
  type        = string
  default     = "5000"
}

variable "client_limit" {
  description = "DFL client limit."
  type        = string
  default     = "2"
}

variable "epoch" {
  description = "DFL epoch value."
  type        = string
  default     = "1"
}

variable "round" {
  description = "DFL round value."
  type        = string
  default     = "1"
}

variable "model_submission_deadline_ms" {
  description = "Model submission deadline in milliseconds."
  type        = string
  default     = "20000"
}

variable "gm_update_timeout_ms" {
  description = "Global-model update timeout in milliseconds."
  type        = string
  default     = "30000"
}

variable "gm_update_timeout_loops" {
  description = "Number of retry loops for global-model updates."
  type        = string
  default     = "2"
}

variable "aggregation_update_estimate_ms" {
  description = "Estimated aggregation update time in milliseconds."
  type        = string
  default     = "30000"
}

variable "gm_update_poll_ms" {
  description = "Polling interval for global-model updates in milliseconds."
  type        = string
  default     = "5000"
}

variable "rsa_private_key_path" {
  description = "Local path to the worker RSA private key; encrypted by the Phala provider before deployment."
  type        = string
  default     = "../data/rsa_keys/private_key.pem"
}

variable "rsa_public_key_path" {
  description = "Local path to the worker RSA public key; delivered with the encrypted app environment."
  type        = string
  default     = "../data/rsa_keys/public_key.pem"
}

variable "initial_gm_signing_key_path" {
  description = "Local path to the initial global-model signing key; encrypted before delivery to the contract-runtime TEE."
  type        = string
  default     = "../data/rsa_keys/private_key.pem"
}

variable "train_images_src" {
  description = "Path inside the container to the training images dataset."
  type        = string
  default     = "/dfl/config/training_data/train-images-0.idx3-ubyte"
}

variable "train_labels_src" {
  description = "Path inside the container to the training labels dataset."
  type        = string
  default     = "/dfl/config/training_data/train-labels-0.idx1-ubyte"
}

variable "test_images_src" {
  description = "Path inside the container to the MNIST test images dataset."
  type        = string
  default     = "/dfl/config/test_data/t10k-images.idx3-ubyte"
}

variable "test_labels_src" {
  description = "Path inside the container to the MNIST test labels dataset."
  type        = string
  default     = "/dfl/config/test_data/t10k-labels.idx1-ubyte"
}

variable "train_data_src" {
  description = "Path inside the container to the ChestMNIST training dataset."
  type        = string
  default     = "/dfl/config/chestmnist/training_data/train-data-0.npz"
}

variable "test_data_src" {
  description = "Path inside the container to the ChestMNIST test dataset."
  type        = string
  default     = "/dfl/config/chestmnist/test_data/test-data.npz"
}

variable "python_service_url" {
  description = "URL for the colocated Python service."
  type        = string
  default     = "http://127.0.0.1:8000"
}

variable "dataset_name" {
  description = "Dataset selector passed into the smart-contract and worker containers."
  type        = string
  default     = "mnist"
}

variable "pccs_fmspc" {
  description = "FMSPC used for PCCS collateral lookup."
  type        = string
  default     = "20A06F000000"
}

variable "pccs_fetch" {
  description = "Whether PCCS collateral should be fetched during startup."
  type        = string
  default     = "1"
}

variable "pccs_tee" {
  description = "PCCS TEE family identifier."
  type        = string
  default     = "tdx"
}

variable "p256_mode" {
  description = "P256 verifier mode for the DCAP deployment flow."
  type        = string
  default     = "fallback"
}

variable "p256_verifier_address" {
  description = "Optional explicit P256 verifier address."
  type        = string
  default     = ""
}

variable "deploy_tdx_v4_dcap" {
  description = "Whether to deploy the TDX v4 DCAP verifier during initialization."
  type        = string
  default     = "1"
}

variable "verify_tdx_quote_onchain" {
  description = "Whether to keep on-chain TDX quote verification enabled in the initialization flow."
  type        = string
  default     = "1"
}

variable "aggregator_timeout_report_percent" {
  description = "Threshold percentage for aggregator timeout reporting."
  type        = string
  default     = "50"
}

variable "keep_alive" {
  description = "Whether the contract-runtime initialization container should stay alive after completion."
  type        = string
  default     = "0"
}

variable "pccs_quote_path" {
  description = "Path inside the contract-runtime container to the reference PCCS quote."
  type        = string
  default     = "../data/phala_tdx_quote"
}

variable "tdx_quote_path" {
  description = "Path inside the contract-runtime container to the generated TDX quote."
  type        = string
  default     = ""
}

variable "tdx_reference_quote_path" {
  description = "Path inside the contract-runtime container to the reference TDX quote."
  type        = string
  default     = ""
}

variable "public_ip" {
  description = "Optional public IP value injected into the worker container."
  type        = string
  default     = ""
}

variable "msg_broker_ip" {
  description = "Optional message broker IP injected into the worker container."
  type        = string
  default     = ""
}

variable "runtime_endpoint_override" {
  description = "Optional explicit Phala runtime endpoint base URL used by worker TEEs instead of the Terraform-managed contract-runtime endpoint."
  type        = string
  default     = null
}

variable "runtime_rpc_url_override" {
  description = "Optional explicit runtime RPC URL used by worker TEEs instead of deriving it from the contract-runtime endpoint."
  type        = string
  default     = null
}

variable "runtime_kubo_api_url_override" {
  description = "Optional explicit runtime Kubo API URL used by worker TEEs instead of deriving it from the contract-runtime endpoint."
  type        = string
  default     = null
}

variable "runtime_kubo_gateway_url_override" {
  description = "Optional explicit runtime Kubo gateway URL used by worker TEEs instead of deriving it from the contract-runtime endpoint."
  type        = string
  default     = null
}
