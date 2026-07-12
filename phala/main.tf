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

locals {
  worker_account_addresses = concat(
    [var.account_address],
    [for worker_key in keys(nonsensitive(var.additional_workers)) : var.additional_workers[worker_key].account_address],
  )

  contracts_compose_content = templatefile("${path.module}/dstack-compose.contracts.phala.tftpl", {
    smart_contracts_image             = var.smart_contracts_image
    anvil_image                       = var.anvil_image
    ipfs_image                        = var.ipfs_image
    w0_account_address                = var.account_address
    w1_account_address                = coalesce(var.runtime_w1_account_address, var.account_address)
    worker_account_addresses          = join(",", local.worker_account_addresses)
    initial_gm_signer_address         = coalesce(var.initial_gm_signer_address, var.account_address)
    blockchain_provider               = var.blockchain_provider
    initial_gm_cid                    = var.initial_gm_cid
    initial_gm_sig_cid                = var.initial_gm_sig_cid
    eth_eur_price                     = var.eth_eur_price
    transaction_cost_csv              = var.transaction_cost_csv
    client_limit                      = var.client_limit
    epoch                             = var.epoch
    round                             = var.round
    model_submission_deadline_ms      = var.model_submission_deadline_ms
    gm_update_timeout_ms              = var.gm_update_timeout_ms
    gm_update_timeout_loops           = var.gm_update_timeout_loops
    aggregation_update_estimate_ms    = var.aggregation_update_estimate_ms
    gm_update_poll_ms                 = var.gm_update_poll_ms
    model_transfer_timeout_ms         = var.model_transfer_timeout_ms
    model_transfer_retry_delay_ms     = var.model_transfer_retry_delay_ms
    train_images_src                  = var.train_images_src
    train_labels_src                  = var.train_labels_src
    python_service_url                = var.python_service_url
    public_ip                         = var.public_ip
    msg_broker_ip                     = var.msg_broker_ip
    dataset_name                      = var.dataset_name
    pccs_fmspc                        = var.pccs_fmspc
    pccs_fetch                        = var.pccs_fetch
    pccs_tee                          = var.pccs_tee
    p256_mode                         = var.p256_mode
    p256_verifier_address             = var.p256_verifier_address
    deploy_tdx_v4_dcap                = var.deploy_tdx_v4_dcap
    verify_tdx_quote_onchain          = var.verify_tdx_quote_onchain
    aggregator_timeout_report_percent = var.aggregator_timeout_report_percent
    worker_image                      = var.worker_image
    keep_alive                        = var.keep_alive
    pccs_quote_path                   = var.pccs_quote_path
    tdx_quote_path                    = var.tdx_quote_path
    tdx_reference_quote_path          = var.tdx_reference_quote_path
    phala_compose_path                = var.phala_compose_path
    phala_app_code_path               = var.phala_app_code_path
    phala_rtmr3_event_log_path        = var.phala_rtmr3_event_log_path
    phala_rtmr3_event_digests         = var.phala_rtmr3_event_digests
    phala_enforce_compose_hash        = var.phala_enforce_compose_hash
    phala_allowed_worker_compose_hashes = var.phala_allowed_worker_compose_hashes
  })

  ssh_authorized_keys = var.ssh_public_key_path == null ? [] : [trimspace(file(var.ssh_public_key_path))]

  contracts_endpoint_base = trimsuffix(coalesce(var.runtime_endpoint_override, phala_app.contract_runtime.endpoint), "/")

  # Phala gateway URLs can already encode the exposed service port in the
  # hostname itself, e.g. https://<app>-5001.dstack-.... In that case we must
  # replace the embedded port marker instead of appending :5001/:8080/:8545.
  contracts_rpc_url = coalesce(
    var.runtime_rpc_url_override,
    can(regex("-[0-9]+\\.", local.contracts_endpoint_base))
    ? replace(local.contracts_endpoint_base, "/-[0-9]+\\./", "-8545.")
    : "${local.contracts_endpoint_base}:8545",
  )
  contracts_kubo_api_url = coalesce(
    var.runtime_kubo_api_url_override,
    can(regex("-[0-9]+\\.", local.contracts_endpoint_base))
    ? replace(local.contracts_endpoint_base, "/-[0-9]+\\./", "-5001.")
    : "${local.contracts_endpoint_base}:5001",
  )
  contracts_kubo_gateway = coalesce(
    var.runtime_kubo_gateway_url_override,
    can(regex("-[0-9]+\\.", local.contracts_endpoint_base))
    ? replace(local.contracts_endpoint_base, "/-[0-9]+\\./", "-8080.")
    : "${local.contracts_endpoint_base}:8080",
  )
  additional_worker_indices = {
    for worker_key in keys(nonsensitive(var.additional_workers)) :
    worker_key => tonumber(replace(worker_key, "worker", ""))
  }
}

resource "phala_ssh_key" "operator" {
  count = var.manage_account_ssh_key && var.ssh_public_key_path != null ? 1 : 0

  name       = var.account_ssh_key_name
  public_key = trimspace(file(var.ssh_public_key_path))
}

resource "phala_app" "contract_runtime" {
  name           = var.contracts_app_name
  docker_compose = local.contracts_compose_content
  env = {
    ETH_WALLET_PRIVATE_KEY                 = var.eth_wallet_private_key != "" ? var.eth_wallet_private_key : var.private_key
    INITIAL_GM_SIGNING_KEY                 = file(var.initial_gm_signing_key_path)
    INITIAL_BOOTSTRAP_RECIPIENT_PUBLIC_KEY = file(var.rsa_public_key_path)
  }
  size = var.contracts_size

  region    = var.region
  image     = var.os_image
  disk_size = var.contracts_disk_size
  replicas  = 1

  kms           = var.kms
  listed        = var.listed
  node_id       = var.node_id
  custom_app_id = var.custom_app_id
  nonce         = var.nonce
  storage_fs    = var.storage_fs

  ssh_authorized_keys = local.ssh_authorized_keys
  pre_launch_script   = var.pre_launch_script

  public_logs     = var.public_logs
  public_sysinfo  = var.public_sysinfo
  public_tcbinfo  = var.public_tcbinfo
  gateway_enabled = var.contracts_gateway_enabled
  secure_time     = var.secure_time

  wait_for_ready       = var.wait_for_ready
  wait_timeout_seconds = var.wait_timeout_seconds
}

resource "phala_app" "dfl_worker" {
  name = var.worker_app_name
  docker_compose = templatefile("${path.module}/dstack-compose.worker.phala.tftpl", {
    worker_image                   = var.worker_image
    account_address                = var.account_address
    rpc_url                        = local.contracts_rpc_url
    kubo_api_url                   = local.contracts_kubo_api_url
    kubo_gateway_url               = local.contracts_kubo_gateway
    client_limit                   = var.client_limit
    epoch                          = var.epoch
    round                          = var.round
    model_submission_deadline_ms   = var.model_submission_deadline_ms
    gm_update_timeout_ms           = var.gm_update_timeout_ms
    gm_update_timeout_loops        = var.gm_update_timeout_loops
    aggregation_update_estimate_ms = var.aggregation_update_estimate_ms
    gm_update_poll_ms              = var.gm_update_poll_ms
    dataset_name                   = var.dataset_name
    train_images_src               = var.train_images_src
    train_labels_src               = var.train_labels_src
    test_images_src                = var.test_images_src
    test_labels_src                = var.test_labels_src
    train_data_src                 = var.train_data_src
    test_data_src                  = var.test_data_src
    python_service_url             = var.python_service_url
    public_ip                      = var.public_ip
    msg_broker_ip                  = var.msg_broker_ip
  })
  env = {
    PRIVATE_KEY     = var.private_key
    RSA_PRIVATE_KEY = file(var.rsa_private_key_path)
    RSA_PUBLIC_KEY  = file(var.rsa_public_key_path)
  }
  size = var.worker_size

  region    = var.region
  image     = var.os_image
  disk_size = var.worker_disk_size
  replicas  = var.worker_replicas

  kms           = var.kms
  listed        = var.listed
  node_id       = var.node_id
  custom_app_id = var.custom_app_id
  nonce         = var.nonce
  storage_fs    = var.storage_fs

  ssh_authorized_keys = local.ssh_authorized_keys
  pre_launch_script   = var.pre_launch_script

  public_logs     = var.public_logs
  public_sysinfo  = var.public_sysinfo
  public_tcbinfo  = var.public_tcbinfo
  gateway_enabled = var.worker_gateway_enabled
  secure_time     = var.secure_time

  wait_for_ready       = var.wait_for_ready
  wait_timeout_seconds = var.wait_timeout_seconds
}

resource "phala_app" "dfl_worker_additional" {
  for_each = toset(keys(nonsensitive(var.additional_workers)))

  name = var.additional_workers[each.key].app_name
  docker_compose = templatefile("${path.module}/dstack-compose.worker.phala.tftpl", {
    worker_image                   = var.worker_image
    account_address                = var.additional_workers[each.key].account_address
    rpc_url                        = local.contracts_rpc_url
    kubo_api_url                   = local.contracts_kubo_api_url
    kubo_gateway_url               = local.contracts_kubo_gateway
    client_limit                   = var.client_limit
    epoch                          = var.epoch
    round                          = var.round
    model_submission_deadline_ms   = var.model_submission_deadline_ms
    gm_update_timeout_ms           = var.gm_update_timeout_ms
    gm_update_timeout_loops        = var.gm_update_timeout_loops
    aggregation_update_estimate_ms = var.aggregation_update_estimate_ms
    gm_update_poll_ms              = var.gm_update_poll_ms
    dataset_name                   = var.dataset_name
    train_images_src               = replace(var.train_images_src, "-0.", format("-%d.", local.additional_worker_indices[each.key]))
    train_labels_src               = replace(var.train_labels_src, "-0.", format("-%d.", local.additional_worker_indices[each.key]))
    test_images_src                = var.test_images_src
    test_labels_src                = var.test_labels_src
    train_data_src                 = replace(var.train_data_src, "-0.", format("-%d.", local.additional_worker_indices[each.key]))
    test_data_src                  = var.test_data_src
    python_service_url             = var.python_service_url
    public_ip                      = var.public_ip
    msg_broker_ip                  = var.msg_broker_ip
  })
  env = {
    PRIVATE_KEY     = var.additional_workers[each.key].private_key
    RSA_PRIVATE_KEY = file(var.additional_workers[each.key].rsa_private_key_path)
    RSA_PUBLIC_KEY  = file(var.additional_workers[each.key].rsa_public_key_path)
  }
  size = var.worker_size

  region    = var.region
  image     = var.os_image
  disk_size = var.worker_disk_size
  replicas  = 1

  kms           = var.kms
  listed        = var.listed
  node_id       = var.node_id
  custom_app_id = var.custom_app_id
  nonce         = var.nonce
  storage_fs    = var.storage_fs

  ssh_authorized_keys = local.ssh_authorized_keys
  pre_launch_script   = var.pre_launch_script

  public_logs     = var.public_logs
  public_sysinfo  = var.public_sysinfo
  public_tcbinfo  = var.public_tcbinfo
  gateway_enabled = var.worker_gateway_enabled
  secure_time     = var.secure_time

  wait_for_ready       = var.wait_for_ready
  wait_timeout_seconds = var.wait_timeout_seconds
}

resource "phala_cvm_power" "dfl_worker" {
  count = var.manage_power_state ? 1 : 0

  cvm_id = phala_app.dfl_worker.primary_cvm_id
  state  = var.desired_power_state

  wait_for_state       = true
  wait_timeout_seconds = var.wait_timeout_seconds
}

resource "phala_cvm_power" "contract_runtime" {
  count = var.manage_power_state ? 1 : 0

  cvm_id = phala_app.contract_runtime.primary_cvm_id
  state  = var.desired_power_state

  wait_for_state       = true
  wait_timeout_seconds = var.wait_timeout_seconds
}

resource "phala_cvm_power" "dfl_worker_additional" {
  for_each = var.manage_power_state ? toset(keys(nonsensitive(var.additional_workers))) : toset([])

  cvm_id = phala_app.dfl_worker_additional[each.key].primary_cvm_id
  state  = var.desired_power_state

  wait_for_state       = true
  wait_timeout_seconds = var.wait_timeout_seconds
}
