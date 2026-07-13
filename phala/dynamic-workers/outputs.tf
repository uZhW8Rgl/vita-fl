output "workers" {
  description = "Non-secret deployment status for every dynamically managed worker."
  value = {
    for key, app in phala_app.worker : key => {
      app_id         = app.app_id
      primary_cvm_id = app.primary_cvm_id
      cvm_ids        = app.cvm_ids
      endpoint       = app.endpoint
      status         = app.status
    }
  }
}

