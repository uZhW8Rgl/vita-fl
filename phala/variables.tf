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

variable "enable_zk_inference" {
  description = "Legacy switch for the separate ZK inference app. It must stay disabled with dstack-derived participant keys until an attested key-delegation protocol exists."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_zk_inference
    error_message = "enable_zk_inference is incompatible with dstack-derived participant keys; keep it false until the separate ZK TEE has an attested delegation protocol."
  }
}

variable "zk_inference_app_name" {
  description = "Name of the separate Phala app that generates and verifies EZKL proofs."
  type        = string
  default     = "master-thesis-zk-inference"
}

variable "expected_gm_storage_address" {
  description = "Compose-measured GMStorage trust-root address expected by TEE inference and DFL workers."
  type        = string
  default     = "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0"

  validation {
    condition     = can(regex("^0x[0-9a-fA-F]{40}$", var.expected_gm_storage_address))
    error_message = "expected_gm_storage_address must be a 20-byte EVM address."
  }
}

variable "expected_device_registry_address" {
  description = "Compose-measured DeviceRegistry trust-root address expected by TEE inference and DFL workers."
  type        = string
  default     = "0x5FbDB2315678afecb367f032d93F642f64180aa3"

  validation {
    condition     = can(regex("^0x[0-9a-fA-F]{40}$", var.expected_device_registry_address))
    error_message = "expected_device_registry_address must be a 20-byte EVM address."
  }
}

variable "expected_chain_id" {
  description = "Compose-measured EVM chain ID expected by TEE inference and DFL workers."
  type        = number
  default     = 31337

  validation {
    condition = (
      var.expected_chain_id >= 1 &&
      floor(var.expected_chain_id) == var.expected_chain_id
    )
    error_message = "expected_chain_id must be a positive integer."
  }
}

variable "expected_aggregator_address" {
  description = "Compose-measured AggregatorSelection trust-root address expected by DFL workers."
  type        = string
  default     = "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512"

  validation {
    condition     = can(regex("^0x[0-9a-fA-F]{40}$", var.expected_aggregator_address))
    error_message = "expected_aggregator_address must be a 20-byte EVM address."
  }
}

variable "expected_medical_signer_registry_address" {
  description = "Compose-measured MedicalSignerRegistry trust-root address expected by DFL workers."
  type        = string
  default     = "0xCf7Ed3AccA5a467e9e704C703E8D87F634fB0Fc9"

  validation {
    condition     = can(regex("^0x[0-9a-fA-F]{40}$", var.expected_medical_signer_registry_address))
    error_message = "expected_medical_signer_registry_address must be a 20-byte EVM address."
  }
}

variable "enable_ollama" {
  description = "Deploy a separate Ollama Phala app for the LLM used by the agent."
  type        = bool
  default     = false
}

variable "ollama_app_name" {
  description = "Name of the separate Phala Cloud app that runs Ollama."
  type        = string
  default     = "master-thesis-ollama"
}

variable "enable_phala_agent" {
  description = "Run the LLM/MCP agent and SCITT-CCF transparency log in the contract-runtime CVM."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_phala_agent || var.enable_phala_ui
    error_message = "enable_phala_agent requires enable_phala_ui so the authenticated UI can proxy the agent API."
  }

  validation {
    condition = (
      !var.enable_phala_agent ||
      var.enable_ollama ||
      var.ollama_base_url_override != null
    )
    error_message = "enable_phala_agent requires enable_ollama or ollama_base_url_override."
  }

}

variable "enable_sello_receipts" {
  description = "Require receiver-signed, owner-encrypted Sello receipts for all six public MCP tool calls."
  type        = bool
  default     = false
}

variable "sello_token_issuer_signing_seed" {
  description = "Base64url or hexadecimal 32-byte Ed25519 seed used by the owner to issue tool authorization tokens."
  type        = string
  sensitive   = true
  default     = ""
}

variable "sello_owner_hpke_private_key" {
  description = "Base64url or hexadecimal 32-byte X25519 private key used by the owner to decrypt tool receipts."
  type        = string
  sensitive   = true
  default     = ""
}

variable "sello_token_issuer_public_key" {
  description = "Base64url or hexadecimal Ed25519 token-issuer public key trusted by inference services."
  type        = string
  default     = ""
}

variable "sello_tee_service_signing_seed" {
  description = "Encrypted-env 32-byte Ed25519 seed held by the TEE inference receiver."
  type        = string
  sensitive   = true
  default     = ""
}

variable "sello_zk_service_signing_seed" {
  description = "Encrypted-env 32-byte Ed25519 seed held by the ZK inference receiver."
  type        = string
  sensitive   = true
  default     = ""
}

variable "sello_service_registry" {
  description = "JSON map from receiver service IDs to their base64url or hexadecimal Ed25519 public keys."
  type        = string
  default     = "{}"
}

variable "sello_scitt_url" {
  description = "Public HTTPS URL of the SCITT service reachable by the separate TEE and ZK receiver CVMs."
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_sello_receipts || can(regex("^https://", var.sello_scitt_url))
    error_message = "sello_scitt_url must be a public HTTPS URL when Sello receipts are enabled."
  }
}

variable "agent_image" {
  description = "Digest-pinned LLM/MCP agent image."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-agent@sha256:c750821699491d541d1a401d4b586e09403330a0b78536869349a1df59b999db"

  validation {
    condition = (
      (!var.enable_phala_agent && !var.enable_ollama) ||
      can(regex("^ghcr\\.io/.+@sha256:[0-9a-f]{64}$", var.agent_image))
    )
    error_message = "agent_image must be digest-pinned when the Phala agent or Ollama proxy is enabled."
  }
}

variable "transparency_log_image" {
  description = "Digest-pinned SCITT-CCF transparency-log image."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-transparency-log@sha256:4c6789921d5ff89e546c65c435bc19d479acc8248715dff3f5b1536e2c8af723"

  validation {
    condition = (
      !var.enable_phala_agent ||
      can(regex("^ghcr\\.io/.+@sha256:[0-9a-f]{64}$", var.transparency_log_image))
    )
    error_message = "transparency_log_image must be digest-pinned when enable_phala_agent is true."
  }
}

variable "ollama_image" {
  description = "Digest-pinned official linux/amd64 Ollama image."
  type        = string
  default     = "docker.io/ollama/ollama@sha256:836a5dc3595fb7cb70d16ffbea9631af882b1bbf8ad278570e5f263e8e600f76"

  validation {
    condition     = can(regex("^docker\\.io/ollama/ollama@sha256:[0-9a-f]{64}$", var.ollama_image))
    error_message = "ollama_image must be a digest-pinned official docker.io/ollama/ollama reference."
  }
}

variable "ollama_model" {
  description = "Ollama model pulled into the fresh LLM CVM at startup."
  type        = string
  default     = "qwen3:1.7b"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+$", var.ollama_model))
    error_message = "ollama_model must be an explicit Ollama name:tag reference."
  }
}

variable "ollama_api_token" {
  description = "Bearer token shared through encrypted Phala app environments between the agent and Ollama proxy."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = !var.enable_ollama || can(regex("^[A-Za-z0-9_-]{24,128}$", var.ollama_api_token))
    error_message = "ollama_api_token must contain 24-128 URL-safe characters when Ollama is enabled."
  }
}

variable "ollama_base_url_override" {
  description = "Optional externally managed Ollama base URL used instead of the Terraform-managed Ollama app."
  type        = string
  default     = null

  validation {
    condition = (
      var.ollama_base_url_override == null ||
      can(regex("^https://[^[:space:]]+$", var.ollama_base_url_override))
    )
    error_message = "ollama_base_url_override must be an HTTPS URL."
  }
}

variable "tee_inference_url_override" {
  description = "Optional public URL for the attested TEE inference service used by the agent."
  type        = string
  default     = null

  validation {
    condition = (
      var.tee_inference_url_override == null ||
      can(regex("^https://[^[:space:]]+$", var.tee_inference_url_override))
    )
    error_message = "tee_inference_url_override must be an HTTPS URL."
  }
}

variable "additional_workers" {
  description = "Additional DFL worker apps with independent account/key pairs."
  type = map(object({
    app_name        = string
    account_address = string
    private_key     = string
  }))
  sensitive = true
  default   = {}
}

variable "dynamic_worker_inventory" {
  description = "Legacy single-entry JSON inventory for small fixed worker pools."
  type        = string
  sensitive   = true
  default     = ""
}

variable "dynamic_worker_inventory_chunks" {
  description = "Encrypted, size-bounded environment chunks containing the fixed W0-W499 worker identities."
  type        = map(string)
  sensitive   = true
  default     = {}

  validation {
    condition = alltrue([
      for name, value in var.dynamic_worker_inventory_chunks :
      can(regex("^DYNAMIC_WORKER_INVENTORY_[0-9]{3}$", name)) && length(value) <= 60000
    ])
    error_message = "Inventory chunk names must use DYNAMIC_WORKER_INVENTORY_NNN and each value must contain at most 60000 bytes."
  }
}

variable "max_dynamic_workers" {
  description = "Maximum number of fixed worker identities exposed to the Phala control API."
  type        = number
  default     = 500

  validation {
    condition     = var.max_dynamic_workers >= 1 && var.max_dynamic_workers <= 500
    error_message = "max_dynamic_workers must be between 1 and 500."
  }
}

variable "initial_dynamic_worker_count" {
  description = "Initially selected worker count in the Control API; the UI may select any count up to max_dynamic_workers."
  type        = number
  default     = 3

  validation {
    condition     = var.initial_dynamic_worker_count >= 1 && var.initial_dynamic_worker_count <= 500
    error_message = "initial_dynamic_worker_count must be between 1 and 500."
  }
}

variable "anvil_account_count" {
  description = "Number of deterministic development accounts funded by the embedded Anvil runtime."
  type        = number
  default     = 500

  validation {
    condition     = var.anvil_account_count >= 1 && var.anvil_account_count <= 500
    error_message = "anvil_account_count must be between 1 and 500."
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
  default     = "ghcr.io/uzhw8rgl/master-thesis-control-api@sha256:05e48039b75445af19622e46e377370a59075ce2cd0bb28e47a885e505301504"

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
  default     = "ghcr.io/uzhw8rgl/master-thesis-ui@sha256:cfa6bf74771b4b16e3bf928ff9f3d7066e924ec728875dbc05cda9e312389a22"

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

variable "zk_inference_size" {
  description = "Phala CVM size slug for the separate ZK inference app."
  type        = string
  default     = "tdx.medium"
}

variable "ollama_size" {
  description = "Phala CVM size slug for the separate Ollama app."
  type        = string
  default     = "tdx.medium"
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

variable "zk_inference_disk_size" {
  description = "Disk size in GB for the separate ZK inference app."
  type        = number
  default     = 20
}

variable "ollama_disk_size" {
  description = "Disk size in GB for the fresh Ollama app."
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

variable "ollama_gateway_enabled" {
  description = "Enable the public gateway endpoint for the separate Ollama app."
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
  default     = "ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:0ee1c63c221ceb052ad7a0f132cb09a7978ea7f2d1b2f84b9ca332df6c8df937"
}

variable "smart_contracts_image" {
  description = "Container image for the smart-contract initialization service."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:10430d2b20f5e8a5f0b04b94c6e158944739ea1de90d18f83e1546e9d782ef48"
}

variable "zk_inference_image" {
  description = "Digest-pinned ZK inference container image."
  type        = string
  default     = "ghcr.io/uzhw8rgl/master-thesis-zk-inference@sha256:7df4d5632e68e4e97c7585130c99c4dd5eeba736c348a792842aba4b9c1ec8ed"

  validation {
    condition = (
      !var.enable_zk_inference ||
      can(regex("^ghcr\\.io/.+@sha256:[0-9a-f]{64}$", var.zk_inference_image))
    )
    error_message = "zk_inference_image must be digest-pinned when ZK inference is enabled."
  }
}

variable "zk_inference_url_override" {
  description = "HTTPS endpoint for the separately deployed ZK inference app consumed by the agent."
  type        = string
  default     = null
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

variable "rsa_private_key" {
  description = "Legacy RSA private key retained only for disabled compatibility paths; workers derive their participant key inside dstack."
  type        = string
  sensitive   = true
}

variable "rsa_public_key" {
  description = "Legacy RSA public key retained only for disabled compatibility paths; workers derive their participant key inside dstack."
  type        = string
  sensitive   = true
}

variable "initial_gm_signing_key" {
  description = "Initial global-model signing key supplied from the selected Phala environment file and encrypted before delivery."
  type        = string
  sensitive   = true
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

variable "bootstrap_min_recipients" {
  description = "Minimum number of live DCAP-registered worker keys required before the initial encrypted model is published."
  type        = number
  default     = 1

  validation {
    condition     = var.bootstrap_min_recipients >= 1 && var.bootstrap_min_recipients <= 500 && floor(var.bootstrap_min_recipients) == var.bootstrap_min_recipients
    error_message = "bootstrap_min_recipients must be an integer between 1 and 500."
  }
}

variable "bootstrap_registration_settle_seconds" {
  description = "Quiet period after the last DeviceRegistry membership change before the live bootstrap recipient set is finalized."
  type        = number
  default     = 30

  validation {
    condition     = var.bootstrap_registration_settle_seconds >= 0 && floor(var.bootstrap_registration_settle_seconds) == var.bootstrap_registration_settle_seconds
    error_message = "bootstrap_registration_settle_seconds must be a non-negative integer."
  }
}

variable "bootstrap_registration_timeout_seconds" {
  description = "Maximum time the contract runtime waits for live DCAP-registered bootstrap recipients."
  type        = number
  default     = 1800

  validation {
    condition     = var.bootstrap_registration_timeout_seconds >= 60 && floor(var.bootstrap_registration_timeout_seconds) == var.bootstrap_registration_timeout_seconds
    error_message = "bootstrap_registration_timeout_seconds must be an integer of at least 60 seconds."
  }
}

variable "bootstrap_registration_poll_seconds" {
  description = "Polling interval while the contract runtime waits for live DeviceRegistry recipients."
  type        = number
  default     = 2

  validation {
    condition     = var.bootstrap_registration_poll_seconds >= 1 && floor(var.bootstrap_registration_poll_seconds) == var.bootstrap_registration_poll_seconds
    error_message = "bootstrap_registration_poll_seconds must be a positive integer."
  }
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
