terraform {
  required_version = ">= 1.5.0"

  required_providers {
    phala = {
      source  = "phala-network/phala"
      version = "0.2.0-beta.3"
    }
  }
}

provider "phala" {
  api_key = var.phala_cloud_api_key
}

resource "phala_app" "worker" {
  for_each = toset(keys(nonsensitive(var.workers)))

  name = var.workers[each.key].app_name
  docker_compose = templatefile("${path.module}/worker-compose.tftpl", {
    worker_image                             = var.worker_image
    account_address                          = var.workers[each.key].account_address
    device_id                                = var.workers[each.key].device_id
    rpc_url                                  = var.rpc_url
    kubo_api_url                             = var.kubo_api_url
    kubo_gateway_url                         = var.kubo_gateway_url
    telemetry_url                            = var.telemetry_url
    expected_device_registry_address         = var.expected_device_registry_address
    expected_aggregator_address              = var.expected_aggregator_address
    expected_gm_storage_address              = var.expected_gm_storage_address
    expected_medical_signer_registry_address = var.expected_medical_signer_registry_address
    expected_chain_id                        = var.expected_chain_id
    epoch                                    = var.epoch
    round                                    = var.round
    gm_update_timeout_ms                     = var.gm_update_timeout_ms
    gm_update_timeout_loops                  = var.gm_update_timeout_loops
    aggregation_update_estimate_ms           = var.aggregation_update_estimate_ms
    gm_update_poll_ms                        = var.gm_update_poll_ms
    dataset_name                             = var.dataset_name
    train_images_src                         = replace(var.train_images_src, "-0.", format("-%d.", var.workers[each.key].device_id))
    train_labels_src                         = replace(var.train_labels_src, "-0.", format("-%d.", var.workers[each.key].device_id))
    test_images_src                          = var.test_images_src
    test_labels_src                          = var.test_labels_src
    train_data_src                           = replace(var.train_data_src, "-0.", format("-%d.", var.workers[each.key].device_id))
    test_data_src                            = var.test_data_src
    python_service_url                       = var.python_service_url
    public_ip                                = var.public_ip
    msg_broker_ip                            = var.msg_broker_ip
    worker_count                             = length(var.workers)
    inference_enabled                        = var.workers[each.key].device_id == 0
    sello_required                           = var.sello_required
    sello_scitt_url                          = var.sello_scitt_url
  })
  env = merge(
    {
      PRIVATE_KEY = var.workers[each.key].private_key
    },
    var.workers[each.key].device_id == 0 ? {
      SELLO_SERVICE_SIGNING_SEED    = var.sello_tee_service_signing_seed
      SELLO_TOKEN_ISSUER_PUBLIC_KEY = var.sello_token_issuer_public_key
    } : {},
  )

  # Dynamic workers deliberately stay on the smallest requested Phala plan.
  size      = "tdx.small"
  disk_size = 20
  replicas  = 1
  region    = var.region
  image     = var.os_image

  kms                 = "phala"
  listed              = false
  storage_fs          = "zfs"
  ssh_authorized_keys = []
  pre_launch_script   = null
  public_logs         = var.public_logs
  public_sysinfo      = var.public_sysinfo
  public_tcbinfo      = var.public_tcbinfo
  gateway_enabled     = true

  wait_for_ready       = true
  wait_timeout_seconds = var.wait_timeout_seconds
}
