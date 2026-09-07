# This resource belongs to the deployment state, not to a cloud application.
# The launcher persists it before changing any apps and excludes it from the
# targeted reset, so a failed recreation can recover exactly the selected pins.
resource "terraform_data" "deployment_manifest" {
  input = {
    images = {
      worker_image           = var.worker_image
      smart_contracts_image  = var.smart_contracts_image
      control_api_image      = var.control_api_image
      ui_image               = var.ui_image
      agent_image            = var.agent_image
      transparency_log_image = var.transparency_log_image
      zk_inference_image     = var.zk_inference_image
    }
    revisions = var.deployment_image_revisions
  }
}
