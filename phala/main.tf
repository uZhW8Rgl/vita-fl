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
  dynamic_worker_inventory_environment = join("\n", [
    for name in sort(nonsensitive(keys(var.dynamic_worker_inventory_chunks))) :
    format("      %s: \"$${%s}\"", name, name)
  ])

  # The runtime RPC endpoint is a fixed worker-policy field. Policy references
  # and live worker app-composes must therefore receive the exact same value.
  # Deriving it from phala_app.contract_runtime.endpoint here would introduce a
  # dependency cycle because the references are injected into that app. The
  # two-pass launcher supplies the explicit override after creating the runtime
  # endpoint and before any dynamic worker is started.
  worker_policy_runtime_rpc_url = coalesce(
    var.runtime_rpc_url_override,
    "http://runtime-endpoint-not-configured.invalid",
  )

  worker_policy_reference_inputs = {
    worker_image                             = var.worker_image
    account_address                          = var.account_address
    rpc_url                                  = local.worker_policy_runtime_rpc_url
    kubo_api_url                             = "http://kubo-policy-reference.invalid"
    kubo_gateway_url                         = "http://gateway-policy-reference.invalid"
    expected_device_registry_address         = var.expected_device_registry_address
    expected_aggregator_address              = var.expected_aggregator_address
    expected_gm_storage_address              = var.expected_gm_storage_address
    expected_medical_signer_registry_address = var.expected_medical_signer_registry_address
    expected_chain_id                        = var.expected_chain_id
    epoch                                    = var.epoch
    round                                    = var.round
    dfl_model_seed                           = var.dfl_model_seed
    dfl_train_seed                           = var.dfl_train_seed
    dfl_train_optimizer                      = var.dfl_train_optimizer
    dfl_train_learning_rate                  = var.dfl_train_learning_rate
    dfl_train_lr_schedule                    = var.dfl_train_lr_schedule
    dfl_train_lr_decay_start_round           = var.dfl_train_lr_decay_start_round
    dfl_train_lr_final_factor                = var.dfl_train_lr_final_factor
    dfl_train_weight_decay                   = var.dfl_train_weight_decay
    dfl_grad_clip_norm                       = var.dfl_grad_clip_norm
    dfl_pos_weight_cap                       = var.dfl_pos_weight_cap
    gm_update_timeout_ms                     = var.gm_update_timeout_ms
    gm_update_timeout_loops                  = var.gm_update_timeout_loops
    aggregation_update_estimate_ms           = var.aggregation_update_estimate_ms
    gm_update_poll_ms                        = var.gm_update_poll_ms
    eth_eur_price                            = var.eth_eur_price
    eth_usd_price                            = var.eth_usd_price
    exchange_rate_source                     = var.exchange_rate_source
    exchange_rate_timestamp_utc              = var.exchange_rate_timestamp_utc
    reference_mainnet_gas_price_gwei         = var.reference_mainnet_gas_price_gwei
    reference_gas_price_source               = var.reference_gas_price_source
    reference_gas_price_timestamp_utc        = var.reference_gas_price_timestamp_utc
    dataset_name                             = var.dataset_name
    train_images_src                         = var.train_images_src
    train_labels_src                         = var.train_labels_src
    test_images_src                          = var.test_images_src
    test_labels_src                          = var.test_labels_src
    train_data_src                           = var.train_data_src
    test_data_src                            = var.test_data_src
    python_service_url                       = var.python_service_url
    public_ip                                = var.public_ip
    msg_broker_ip                            = var.msg_broker_ip
    device_id                                = 0
    worker_count                             = var.max_dynamic_workers
    telemetry_url                            = "http://telemetry-policy-reference.invalid"
    sello_required                           = var.enable_sello_receipts
    sello_scitt_url                          = var.sello_scitt_url
  }

  training_worker_policy_compose = templatefile(
    "${path.module}/dynamic-workers/worker-compose.tftpl",
    merge(local.worker_policy_reference_inputs, { inference_enabled = false }),
  )
  inference_worker_policy_compose = templatefile(
    "${path.module}/dynamic-workers/worker-compose.tftpl",
    merge(local.worker_policy_reference_inputs, { inference_enabled = true }),
  )
  training_worker_policy_app_compose = jsonencode({
    docker_compose_file = local.training_worker_policy_compose
    manifest_version    = 2
    name                = "training-worker-policy-reference"
    runner              = "docker-compose"
  })
  inference_worker_policy_app_compose = jsonencode({
    docker_compose_file = local.inference_worker_policy_compose
    manifest_version    = 2
    name                = "inference-worker-policy-reference"
    runner              = "docker-compose"
  })

  worker_account_addresses = distinct(concat(
    [var.account_address],
    [for worker_key in keys(nonsensitive(var.additional_workers)) : var.additional_workers[worker_key].account_address],
  ))

  contracts_compose_content = templatefile("${path.module}/dstack-compose.contracts.phala.tftpl", {
    smart_contracts_image                    = var.smart_contracts_image
    anvil_image                              = var.anvil_image
    anvil_account_count                      = var.anvil_account_count
    ipfs_image                               = var.ipfs_image
    w0_account_address                       = var.account_address
    w1_account_address                       = coalesce(var.runtime_w1_account_address, var.account_address)
    worker_account_addresses                 = join(",", local.worker_account_addresses)
    blockchain_provider                      = var.blockchain_provider
    initial_gm_cid                           = var.initial_gm_cid
    eth_eur_price                            = var.eth_eur_price
    eth_usd_price                            = var.eth_usd_price
    exchange_rate_source                     = var.exchange_rate_source
    exchange_rate_timestamp_utc              = var.exchange_rate_timestamp_utc
    reference_mainnet_gas_price_gwei         = var.reference_mainnet_gas_price_gwei
    reference_gas_price_source               = var.reference_gas_price_source
    reference_gas_price_timestamp_utc        = var.reference_gas_price_timestamp_utc
    transaction_cost_csv                     = var.transaction_cost_csv
    client_limit                             = var.client_limit
    epoch                                    = var.epoch
    round                                    = var.round
    dfl_model_seed                           = var.dfl_model_seed
    dfl_train_seed                           = var.dfl_train_seed
    dfl_train_optimizer                      = var.dfl_train_optimizer
    dfl_train_learning_rate                  = var.dfl_train_learning_rate
    dfl_train_lr_schedule                    = var.dfl_train_lr_schedule
    dfl_train_lr_decay_start_round           = var.dfl_train_lr_decay_start_round
    dfl_train_lr_final_factor                = var.dfl_train_lr_final_factor
    dfl_train_weight_decay                   = var.dfl_train_weight_decay
    dfl_grad_clip_norm                       = var.dfl_grad_clip_norm
    dfl_pos_weight_cap                       = var.dfl_pos_weight_cap
    model_submission_deadline_ms             = var.model_submission_deadline_ms
    gm_update_timeout_ms                     = var.gm_update_timeout_ms
    gm_update_timeout_loops                  = var.gm_update_timeout_loops
    aggregation_update_estimate_ms           = var.aggregation_update_estimate_ms
    gm_update_poll_ms                        = var.gm_update_poll_ms
    model_transfer_timeout_ms                = var.model_transfer_timeout_ms
    model_transfer_retry_delay_ms            = var.model_transfer_retry_delay_ms
    train_images_src                         = var.train_images_src
    train_labels_src                         = var.train_labels_src
    test_images_src                          = var.test_images_src
    test_labels_src                          = var.test_labels_src
    train_data_src                           = var.train_data_src
    test_data_src                            = var.test_data_src
    python_service_url                       = var.python_service_url
    public_ip                                = var.public_ip
    msg_broker_ip                            = var.msg_broker_ip
    dataset_name                             = var.dataset_name
    pccs_fmspc                               = var.pccs_fmspc
    pccs_fetch                               = var.pccs_fetch
    pccs_tee                                 = var.pccs_tee
    p256_mode                                = var.p256_mode
    p256_verifier_address                    = var.p256_verifier_address
    deploy_tdx_v4_dcap                       = var.deploy_tdx_v4_dcap
    verify_tdx_quote_onchain                 = var.verify_tdx_quote_onchain
    aggregator_timeout_report_percent        = var.aggregator_timeout_report_percent
    worker_image                             = var.worker_image
    pccs_quote_path                          = var.pccs_quote_path
    tdx_quote_path                           = var.tdx_quote_path
    tdx_reference_quote_path                 = var.tdx_reference_quote_path
    enable_control_api                       = var.enable_phala_control_api
    control_api_image                        = var.control_api_image
    enable_ui                                = var.enable_phala_ui
    ui_image                                 = var.ui_image
    enable_agent                             = var.enable_phala_agent
    agent_image                              = var.agent_image
    transparency_log_image                   = var.transparency_log_image
    ollama_base_url                          = local.agent_ollama_base_url
    ollama_model                             = var.ollama_model
    tee_inference_url                        = local.agent_tee_inference_url
    tee_inference_image_digest               = replace(var.worker_image, "/^.*@/", "")
    expected_gm_storage_address              = var.expected_gm_storage_address
    expected_device_registry_address         = var.expected_device_registry_address
    expected_chain_id                        = var.expected_chain_id
    expected_aggregator_address              = var.expected_aggregator_address
    expected_medical_signer_registry_address = var.expected_medical_signer_registry_address
    expected_runtime_rpc_url                 = local.worker_policy_runtime_rpc_url
    enable_zk_inference = var.enable_zk_inference
    zk_inference_url = var.enable_zk_inference && var.zk_inference_url_override != null ? trimsuffix(
      var.zk_inference_url_override,
      "/",
    ) : ""
    agent_available                      = var.enable_phala_agent ? "1" : "0"
    dynamic_worker_inventory             = var.dynamic_worker_inventory
    dynamic_worker_inventory_environment = local.dynamic_worker_inventory_environment
    dynamic_worker_rpc_url               = var.runtime_rpc_url_override == null ? "" : var.runtime_rpc_url_override
    dynamic_worker_kubo_api_url          = var.runtime_kubo_api_url_override == null ? "" : var.runtime_kubo_api_url_override
    dynamic_worker_kubo_gateway_url      = var.runtime_kubo_gateway_url_override == null ? "" : var.runtime_kubo_gateway_url_override
    max_dynamic_workers                  = var.max_dynamic_workers
    initial_dynamic_worker_count         = min(var.initial_dynamic_worker_count, var.max_dynamic_workers)
    region                               = var.region
    os_image                             = var.dynamic_worker_os_image
    node_id                              = var.dynamic_worker_node_id == null ? "" : tostring(var.dynamic_worker_node_id)
    sello_required                       = var.enable_sello_receipts ? "1" : "0"
    sello_scitt_url                      = var.sello_scitt_url
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
  ollama_endpoint_base = var.enable_ollama ? trimsuffix(phala_app.ollama[0].endpoint, "/") : ""
  agent_ollama_base_url = var.ollama_base_url_override != null ? trimsuffix(
    var.ollama_base_url_override,
    "/",
  ) : local.ollama_endpoint_base
  agent_tee_inference_url = var.tee_inference_url_override != null ? trimsuffix(
    var.tee_inference_url_override,
    "/",
  ) : ""
  inference_runtime_rpc_url = local.worker_policy_runtime_rpc_url
  inference_runtime_kubo_api_url = coalesce(
    var.runtime_kubo_api_url_override,
    "http://runtime-endpoint-not-configured.invalid",
  )
  inference_runtime_kubo_gateway_url = coalesce(
    var.runtime_kubo_gateway_url_override,
    "http://runtime-endpoint-not-configured.invalid",
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
  env = merge({
    ETH_WALLET_PRIVATE_KEY                  = var.eth_wallet_private_key != "" ? var.eth_wallet_private_key : var.private_key
    DYNAMIC_WORKER_INVENTORY                = var.dynamic_worker_inventory
    TRAINING_WORKER_POLICY_APP_COMPOSE_B64  = base64encode(local.training_worker_policy_app_compose)
    INFERENCE_WORKER_POLICY_APP_COMPOSE_B64 = base64encode(local.inference_worker_policy_app_compose)
    }, var.dynamic_worker_inventory_chunks, var.enable_phala_control_api ? {
    PHALA_CLOUD_API_KEY            = var.phala_cloud_api_key
    CONTROL_ADMIN_TOKEN            = var.control_admin_token
    SELLO_TOKEN_ISSUER_PUBLIC_KEY  = var.sello_token_issuer_public_key
    } : {}, var.enable_phala_ui ? {
    UI_BASIC_AUTH_USERNAME = var.ui_basic_auth_username
    UI_BASIC_AUTH_PASSWORD = var.ui_basic_auth_password
    } : {}, var.enable_phala_agent ? {
    OLLAMA_API_TOKEN                = var.ollama_api_token
    SELLO_TOKEN_ISSUER_SIGNING_SEED = var.sello_token_issuer_signing_seed
    SELLO_OWNER_HPKE_PRIVATE_KEY    = var.sello_owner_hpke_private_key
    SELLO_SERVICE_REGISTRY          = var.sello_service_registry
  } : {})
  size = var.contracts_size

  region    = var.region
  image     = var.contracts_os_image != null ? var.contracts_os_image : var.os_image
  disk_size = var.contracts_disk_size
  replicas  = var.contracts_replicas

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

  wait_for_ready       = var.contracts_wait_for_ready
  wait_timeout_seconds = var.wait_timeout_seconds
}

resource "phala_app" "dfl_worker" {
  count = var.enable_phala_control_api ? 0 : 1

  name = var.worker_app_name
  docker_compose = templatefile("${path.module}/dstack-compose.worker.phala.tftpl", {
    worker_image                             = var.worker_image
    account_address                          = var.account_address
    rpc_url                                  = local.contracts_rpc_url
    kubo_api_url                             = local.contracts_kubo_api_url
    kubo_gateway_url                         = local.contracts_kubo_gateway
    expected_device_registry_address         = var.expected_device_registry_address
    expected_aggregator_address              = var.expected_aggregator_address
    expected_gm_storage_address              = var.expected_gm_storage_address
    expected_medical_signer_registry_address = var.expected_medical_signer_registry_address
    expected_chain_id                        = var.expected_chain_id
    epoch                                    = var.epoch
    round                                    = var.round
    dfl_model_seed                           = var.dfl_model_seed
    dfl_train_seed                           = var.dfl_train_seed
    dfl_train_optimizer                      = var.dfl_train_optimizer
    dfl_train_learning_rate                  = var.dfl_train_learning_rate
    dfl_train_lr_schedule                    = var.dfl_train_lr_schedule
    dfl_train_lr_decay_start_round           = var.dfl_train_lr_decay_start_round
    dfl_train_lr_final_factor                = var.dfl_train_lr_final_factor
    dfl_train_weight_decay                   = var.dfl_train_weight_decay
    dfl_grad_clip_norm                       = var.dfl_grad_clip_norm
    dfl_pos_weight_cap                       = var.dfl_pos_weight_cap
    gm_update_timeout_ms                     = var.gm_update_timeout_ms
    gm_update_timeout_loops                  = var.gm_update_timeout_loops
    aggregation_update_estimate_ms           = var.aggregation_update_estimate_ms
    gm_update_poll_ms                        = var.gm_update_poll_ms
    eth_eur_price                            = var.eth_eur_price
    eth_usd_price                            = var.eth_usd_price
    exchange_rate_source                     = var.exchange_rate_source
    exchange_rate_timestamp_utc              = var.exchange_rate_timestamp_utc
    reference_mainnet_gas_price_gwei         = var.reference_mainnet_gas_price_gwei
    reference_gas_price_source               = var.reference_gas_price_source
    reference_gas_price_timestamp_utc        = var.reference_gas_price_timestamp_utc
    dataset_name                             = var.dataset_name
    train_images_src                         = var.train_images_src
    train_labels_src                         = var.train_labels_src
    test_images_src                          = var.test_images_src
    test_labels_src                          = var.test_labels_src
    train_data_src                           = var.train_data_src
    test_data_src                            = var.test_data_src
    python_service_url                       = var.python_service_url
    public_ip                                = var.public_ip
    msg_broker_ip                            = var.msg_broker_ip
    device_id                                = 0
    worker_count                             = 1 + length(var.additional_workers)
    inference_enabled                        = true
    sello_required                           = var.enable_sello_receipts
    sello_scitt_url                          = var.sello_scitt_url
  })
  env = {
    PRIVATE_KEY                    = var.private_key
    SELLO_TOKEN_ISSUER_PUBLIC_KEY = var.sello_token_issuer_public_key
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

  ssh_authorized_keys = []
  pre_launch_script   = null

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
    worker_image                             = var.worker_image
    account_address                          = var.additional_workers[each.key].account_address
    rpc_url                                  = local.contracts_rpc_url
    kubo_api_url                             = local.contracts_kubo_api_url
    kubo_gateway_url                         = local.contracts_kubo_gateway
    expected_device_registry_address         = var.expected_device_registry_address
    expected_aggregator_address              = var.expected_aggregator_address
    expected_gm_storage_address              = var.expected_gm_storage_address
    expected_medical_signer_registry_address = var.expected_medical_signer_registry_address
    expected_chain_id                        = var.expected_chain_id
    epoch                                    = var.epoch
    round                                    = var.round
    dfl_model_seed                           = var.dfl_model_seed
    dfl_train_seed                           = var.dfl_train_seed
    dfl_train_optimizer                      = var.dfl_train_optimizer
    dfl_train_learning_rate                  = var.dfl_train_learning_rate
    dfl_train_lr_schedule                    = var.dfl_train_lr_schedule
    dfl_train_lr_decay_start_round           = var.dfl_train_lr_decay_start_round
    dfl_train_lr_final_factor                = var.dfl_train_lr_final_factor
    dfl_train_weight_decay                   = var.dfl_train_weight_decay
    dfl_grad_clip_norm                       = var.dfl_grad_clip_norm
    dfl_pos_weight_cap                       = var.dfl_pos_weight_cap
    gm_update_timeout_ms                     = var.gm_update_timeout_ms
    gm_update_timeout_loops                  = var.gm_update_timeout_loops
    aggregation_update_estimate_ms           = var.aggregation_update_estimate_ms
    gm_update_poll_ms                        = var.gm_update_poll_ms
    eth_eur_price                            = var.eth_eur_price
    eth_usd_price                            = var.eth_usd_price
    exchange_rate_source                     = var.exchange_rate_source
    exchange_rate_timestamp_utc              = var.exchange_rate_timestamp_utc
    reference_mainnet_gas_price_gwei         = var.reference_mainnet_gas_price_gwei
    reference_gas_price_source               = var.reference_gas_price_source
    reference_gas_price_timestamp_utc        = var.reference_gas_price_timestamp_utc
    dataset_name                             = var.dataset_name
    train_images_src                         = replace(var.train_images_src, "-0.", format("-%d.", local.additional_worker_indices[each.key]))
    train_labels_src                         = replace(var.train_labels_src, "-0.", format("-%d.", local.additional_worker_indices[each.key]))
    test_images_src                          = var.test_images_src
    test_labels_src                          = var.test_labels_src
    train_data_src                           = replace(var.train_data_src, "-0.", format("-%d.", local.additional_worker_indices[each.key]))
    test_data_src                            = var.test_data_src
    python_service_url                       = var.python_service_url
    public_ip                                = var.public_ip
    msg_broker_ip                            = var.msg_broker_ip
    device_id                                = local.additional_worker_indices[each.key]
    worker_count                             = 1 + length(var.additional_workers)
    inference_enabled                        = false
    sello_required                           = false
    sello_scitt_url                          = ""
  })
  env = {
    PRIVATE_KEY = var.additional_workers[each.key].private_key
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

  ssh_authorized_keys = []
  pre_launch_script   = null

  public_logs     = var.public_logs
  public_sysinfo  = var.public_sysinfo
  public_tcbinfo  = var.public_tcbinfo
  gateway_enabled = var.worker_gateway_enabled
  secure_time     = var.secure_time

  wait_for_ready       = var.wait_for_ready
  wait_timeout_seconds = var.wait_timeout_seconds
}

resource "phala_app" "zk_inference" {
  count = var.enable_zk_inference ? 1 : 0

  name = var.zk_inference_app_name
  docker_compose = templatefile("${path.module}/dstack-compose.zk-inference.phala.tftpl", {
    zk_inference_image               = var.zk_inference_image
    account_address                  = var.account_address
    rpc_url                          = local.inference_runtime_rpc_url
    kubo_api_url                     = local.inference_runtime_kubo_api_url
    kubo_gateway_url                 = local.inference_runtime_kubo_gateway_url
    expected_gm_storage_address      = var.expected_gm_storage_address
    expected_device_registry_address = var.expected_device_registry_address
    expected_chain_id                = var.expected_chain_id
    sello_required                   = var.enable_sello_receipts ? "1" : "0"
    sello_scitt_url                  = var.sello_scitt_url
  })
  env = {
    RSA_PRIVATE_KEY               = var.rsa_private_key
    RSA_PUBLIC_KEY                = var.rsa_public_key
    SELLO_SERVICE_SIGNING_SEED    = var.sello_zk_service_signing_seed
    SELLO_TOKEN_ISSUER_PUBLIC_KEY = var.sello_token_issuer_public_key
  }
  size      = var.zk_inference_size
  region    = var.region
  image     = var.os_image
  disk_size = var.zk_inference_disk_size
  replicas  = 1

  kms                  = var.kms
  listed               = var.listed
  node_id              = var.node_id
  custom_app_id        = var.custom_app_id
  nonce                = var.nonce
  storage_fs           = var.storage_fs
  ssh_authorized_keys  = local.ssh_authorized_keys
  pre_launch_script    = var.pre_launch_script
  public_logs          = var.public_logs
  public_sysinfo       = var.public_sysinfo
  public_tcbinfo       = var.public_tcbinfo
  gateway_enabled      = true
  secure_time          = var.secure_time
  wait_for_ready       = var.wait_for_ready
  wait_timeout_seconds = var.wait_timeout_seconds
}

resource "phala_app" "ollama" {
  count = var.enable_ollama ? 1 : 0

  name = var.ollama_app_name
  docker_compose = templatefile("${path.module}/dstack-compose.ollama.phala.tftpl", {
    ollama_image       = var.ollama_image
    ollama_proxy_image = var.agent_image
    ollama_model       = var.ollama_model
  })
  env = {
    OLLAMA_PROXY_TOKEN = var.ollama_api_token
  }
  size = var.ollama_size

  region    = var.region
  image     = var.os_image
  disk_size = var.ollama_disk_size
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
  gateway_enabled = var.ollama_gateway_enabled
  secure_time     = var.secure_time

  wait_for_ready       = var.wait_for_ready
  wait_timeout_seconds = var.wait_timeout_seconds
}

resource "phala_cvm_power" "dfl_worker" {
  count = var.manage_power_state && !var.enable_phala_control_api ? 1 : 0

  cvm_id = phala_app.dfl_worker[0].primary_cvm_id
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

resource "phala_cvm_power" "zk_inference" {
  count = var.enable_zk_inference && var.manage_power_state ? 1 : 0

  cvm_id = phala_app.zk_inference[0].primary_cvm_id
  state  = var.desired_power_state
}

resource "phala_cvm_power" "ollama" {
  count = var.enable_ollama && var.manage_power_state ? 1 : 0

  cvm_id = phala_app.ollama[0].primary_cvm_id
  state  = var.desired_power_state

  wait_for_state       = true
  wait_timeout_seconds = var.wait_timeout_seconds
}
