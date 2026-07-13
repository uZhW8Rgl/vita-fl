output "contracts_app_id" {
  description = "Phala application ID for the contract-runtime TEE."
  value       = phala_app.contract_runtime.app_id
}

output "contracts_endpoint" {
  description = "Public endpoint for the contract-runtime TEE."
  value       = phala_app.contract_runtime.endpoint
}

output "contracts_primary_cvm_id" {
  description = "Primary CVM identifier for the contract-runtime TEE."
  value       = phala_app.contract_runtime.primary_cvm_id
}

output "ui_endpoint" {
  description = "Public browser UI endpoint when the UI is enabled."
  value = var.enable_phala_ui ? (
    can(regex("-[0-9]+\\.", local.contracts_endpoint_base))
    ? replace(local.contracts_endpoint_base, "/-[0-9]+\\./", "-8088.")
    : "${local.contracts_endpoint_base}:8088"
  ) : null
}

output "grafana_endpoint" {
  description = "Public Grafana endpoint when the Phala UI and observability stack are enabled."
  value = var.enable_phala_ui ? (
    can(regex("-[0-9]+\\.", local.contracts_endpoint_base))
    ? replace(local.contracts_endpoint_base, "/-[0-9]+\\./", "-3000.")
    : "${local.contracts_endpoint_base}:3000"
  ) : null
}

output "worker_app_id" {
  description = "Phala application ID for the worker TEE."
  value       = try(phala_app.dfl_worker[0].app_id, null)
}

output "worker_primary_cvm_id" {
  description = "Primary CVM identifier for the worker TEE."
  value       = try(phala_app.dfl_worker[0].primary_cvm_id, null)
}

output "worker_cvm_ids" {
  description = "All CVM identifiers attached to the worker app."
  value       = try(phala_app.dfl_worker[0].cvm_ids, null)
}

output "worker_endpoint" {
  description = "Public endpoint for the worker app, if enabled."
  value       = try(phala_app.dfl_worker[0].endpoint, null)
}

output "worker_status" {
  description = "Current deployment status for the worker app."
  value       = try(phala_app.dfl_worker[0].status, null)
}

output "tee_inference_app_id" {
  description = "Phala application ID for the TEE inference app, when enabled."
  value       = try(phala_app.tee_inference[0].app_id, null)
}

output "tee_inference_primary_cvm_id" {
  description = "Primary CVM identifier for the TEE inference app, when enabled."
  value       = try(phala_app.tee_inference[0].primary_cvm_id, null)
}

output "tee_inference_endpoint" {
  description = "Public endpoint for the TEE inference app, when enabled."
  value       = try(phala_app.tee_inference[0].endpoint, null)
}

output "tee_inference_status" {
  description = "Deployment status for the TEE inference app, when enabled."
  value       = try(phala_app.tee_inference[0].status, null)
}

output "additional_worker_app_ids" {
  description = "Phala application IDs for additional worker apps."
  value       = { for key, app in phala_app.dfl_worker_additional : key => app.app_id }
}

output "additional_worker_cvm_ids" {
  description = "CVM identifiers for additional worker apps."
  value       = { for key, app in phala_app.dfl_worker_additional : key => app.cvm_ids }
}

output "all_worker_app_ids" {
  description = "Phala application IDs for statically managed worker TEEs."
  value = merge(
    var.enable_phala_control_api ? {} : { worker0 = phala_app.dfl_worker[0].app_id },
    { for key, app in phala_app.dfl_worker_additional : key => app.app_id }
  )
}

output "all_worker_cvm_ids" {
  description = "CVM identifiers for statically managed worker TEEs."
  value = merge(
    var.enable_phala_control_api ? {} : { worker0 = phala_app.dfl_worker[0].cvm_ids },
    { for key, app in phala_app.dfl_worker_additional : key => app.cvm_ids }
  )
}

output "all_worker_statuses" {
  description = "Current deployment status for statically managed worker TEEs."
  value = merge(
    var.enable_phala_control_api ? {} : { worker0 = phala_app.dfl_worker[0].status },
    { for key, app in phala_app.dfl_worker_additional : key => app.status }
  )
}

output "account_ssh_key_id" {
  description = "ID of the optional account-level SSH key."
  value       = var.manage_account_ssh_key && var.ssh_public_key_path != null ? phala_ssh_key.operator[0].id : null
}
