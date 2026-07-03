terraform {
  required_version = ">= 1.5.0"

  required_providers {
    phala = {
      source  = "phala-network/phala"
      version = "0.2.0-beta.1"
    }
  }
}

provider "phala" {
  api_key = var.phala_cloud_api_key
}

variable "phala_cloud_api_key" {
  type      = string
  sensitive = true
  default   = null
}

resource "phala_app" "legacy_worker_phala" {
  name           = "master-thesis-dfl-worker-phala"
  docker_compose = <<-EOT
services:
  noop:
    image: alpine:3.20
    command: ["sh", "-lc", "sleep infinity"]
EOT
  size           = "tdx.small"

  region    = "US-WEST-1"
  image     = "dstack-dev-0.5.7"
  disk_size = 20
  replicas  = 1
}

resource "phala_app" "legacy_worker" {
  name           = "master-thesis-dfl-worker"
  docker_compose = <<-EOT
services:
  noop:
    image: alpine:3.20
    command: ["sh", "-lc", "sleep infinity"]
EOT
  size           = "tdx.small"

  region    = "US-WEST-1"
  image     = "dstack-dev-0.5.7"
  disk_size = 20
  replicas  = 1
}
