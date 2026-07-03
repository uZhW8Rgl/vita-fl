variable "phala_cloud_api_key" {
  description = "Optional explicit API key. Prefer the PHALA_CLOUD_API_KEY environment variable."
  type        = string
  sensitive   = true
  default     = null
}

variable "contracts_app_name" {
  description = "Name of the Phala Cloud app that runs anvil, ipfs, and smart-contract initialization."
  type        = string
  default     = "master-thesis-contract-runtime"
}

variable "worker_app_name" {
  description = "Name of the Phala Cloud app that runs the DFL worker."
  type        = string
  default     = "master-thesis-dfl-worker"
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

variable "region" {
  description = "Phala region slug."
  type        = string
  default     = "US-WEST-1"
}

variable "os_image" {
  description = "Phala OS image slug for the CVM."
  type        = string
  default     = "dstack-dev-0.5.7-9b6a5239"
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

variable "replicas" {
  description = "Number of CVM replicas."
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
  default     = null
}

variable "pre_launch_script" {
  description = "Optional shell script executed before the CVM starts."
  type        = string
  default     = null
}

variable "public_logs" {
  description = "Expose container logs publicly."
  type        = bool
  default     = false
}

variable "public_sysinfo" {
  description = "Expose system information publicly."
  type        = bool
  default     = false
}

variable "public_tcbinfo" {
  description = "Expose TCB attestation information publicly."
  type        = bool
  default     = false
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
  default     = false
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
  default     = "ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:8d730338ae80a57da47b4d246910b90ad72f9ef48ccd361cf8d9f305626942de"
}

variable "smart_contracts_image" {
  description = "Container image for the smart-contract initialization service."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-smart-contracts:latest"
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

variable "registry_address" {
  description = "Registry contract address."
  type        = string
}

variable "aggregator_address" {
  description = "Aggregator contract address."
  type        = string
}

variable "gm_storage_address" {
  description = "Global-model storage contract address."
  type        = string
}

variable "sepolia_rpc_url" {
  description = "Sepolia RPC endpoint."
  type        = string
  sensitive   = true
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

variable "rsa_private_key_file" {
  description = "Path inside the container to the RSA private key."
  type        = string
  default     = "/dfl/keys/private_key.pem"
}

variable "rsa_public_key_file" {
  description = "Path inside the container to the RSA public key."
  type        = string
  default     = "/dfl/keys/public_key.pem"
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
  default     = "native"
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
