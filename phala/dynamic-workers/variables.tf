variable "phala_cloud_api_key" {
  type      = string
  sensitive = true
}

variable "workers" {
  description = "Fixed W0-W19 identities selected for this dynamic worker set."
  type = map(object({
    app_name        = string
    account_address = string
    private_key     = string
    rsa_private_key = string
    rsa_public_key  = string
    device_id       = number
  }))
  sensitive = true

  validation {
    condition     = length(var.workers) <= 20
    error_message = "At most 20 fixed worker identities may be selected."
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

