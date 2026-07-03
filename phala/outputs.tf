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

output "worker_app_id" {
  description = "Phala application ID for the worker TEE."
  value       = phala_app.dfl_worker.app_id
}

output "worker_primary_cvm_id" {
  description = "Primary CVM identifier for the worker TEE."
  value       = phala_app.dfl_worker.primary_cvm_id
}

output "worker_cvm_ids" {
  description = "All CVM identifiers attached to the worker app."
  value       = phala_app.dfl_worker.cvm_ids
}

output "worker_endpoint" {
  description = "Public endpoint for the worker app, if enabled."
  value       = phala_app.dfl_worker.endpoint
}

output "worker_status" {
  description = "Current deployment status for the worker app."
  value       = phala_app.dfl_worker.status
}

output "additional_worker_app_ids" {
  description = "Phala application IDs for additional worker apps."
  value       = { for key, app in phala_app.dfl_worker_additional : key => app.app_id }
}

output "additional_worker_cvm_ids" {
  description = "CVM identifiers for additional worker apps."
  value       = { for key, app in phala_app.dfl_worker_additional : key => app.cvm_ids }
}

output "account_ssh_key_id" {
  description = "ID of the optional account-level SSH key."
  value       = var.manage_account_ssh_key && var.ssh_public_key_path != null ? phala_ssh_key.operator[0].id : null
}
