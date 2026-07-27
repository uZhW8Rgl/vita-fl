variable "phala_cloud_api_key" {
  type      = string
  sensitive = true
}

variable "workers" {
  description = "Fixed W0-W499 identities selected for this dynamic worker set."
  type = map(object({
    app_name        = string
    account_address = string
    private_key     = string
    device_id       = number
  }))
  sensitive = true

  validation {
    condition     = length(var.workers) <= 500
    error_message = "At most 500 fixed worker identities may be selected."
  }
}

variable "worker_image" {
  type = string

  validation {
    condition     = can(regex("^ghcr\\.io/.+@sha256:[0-9a-f]{64}$", var.worker_image))
    error_message = "worker_image must be a digest-pinned ghcr.io reference."
  }
}

variable "rpc_url" { type = string }
variable "kubo_api_url" { type = string }
variable "kubo_gateway_url" { type = string }
variable "telemetry_url" { type = string }

variable "expected_device_registry_address" {
  type = string

  validation {
    condition     = can(regex("^0x[0-9a-fA-F]{40}$", var.expected_device_registry_address))
    error_message = "expected_device_registry_address must be a 20-byte EVM address."
  }
}

variable "expected_aggregator_address" {
  type = string

  validation {
    condition     = can(regex("^0x[0-9a-fA-F]{40}$", var.expected_aggregator_address))
    error_message = "expected_aggregator_address must be a 20-byte EVM address."
  }
}

variable "expected_gm_storage_address" {
  type = string

  validation {
    condition     = can(regex("^0x[0-9a-fA-F]{40}$", var.expected_gm_storage_address))
    error_message = "expected_gm_storage_address must be a 20-byte EVM address."
  }
}

variable "expected_medical_signer_registry_address" {
  type = string

  validation {
    condition     = can(regex("^0x[0-9a-fA-F]{40}$", var.expected_medical_signer_registry_address))
    error_message = "expected_medical_signer_registry_address must be a 20-byte EVM address."
  }
}

variable "expected_chain_id" {
  type = number

  validation {
    condition     = var.expected_chain_id >= 1 && floor(var.expected_chain_id) == var.expected_chain_id
    error_message = "expected_chain_id must be a positive integer."
  }
}

variable "region" {
  type    = string
  default = "US-WEST-1"
}

variable "os_image" {
  type    = string
  default = "dstack-dev-0.5.7"
}

variable "client_limit" { type = number }
variable "epoch" { type = number }
variable "round" { type = number }
variable "model_submission_deadline_ms" { type = number }
variable "gm_update_timeout_ms" { type = number }
variable "gm_update_timeout_loops" { type = number }
variable "aggregation_update_estimate_ms" { type = number }
variable "gm_update_poll_ms" { type = number }

variable "dataset_name" {
  type    = string
  default = "chestmnist"
}

variable "train_images_src" { type = string }
variable "train_labels_src" { type = string }
variable "test_images_src" { type = string }
variable "test_labels_src" { type = string }
variable "train_data_src" { type = string }
variable "test_data_src" { type = string }
variable "python_service_url" { type = string }
variable "public_ip" { type = string }
variable "msg_broker_ip" { type = string }

variable "sello_required" {
  type    = bool
  default = false
}

variable "sello_scitt_url" {
  type    = string
  default = ""
}

variable "sello_tee_service_signing_seed" {
  type      = string
  sensitive = true
  default   = ""
}

variable "sello_token_issuer_public_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "public_logs" {
  type    = bool
  default = true
}

variable "public_sysinfo" {
  type    = bool
  default = true
}

variable "public_tcbinfo" {
  type    = bool
  default = true
}

variable "wait_timeout_seconds" {
  type    = number
  default = 900
}
