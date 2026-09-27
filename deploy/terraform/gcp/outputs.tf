output "cluster_name" { value = google_container_cluster.ramen.name }
output "cluster_location" { value = google_container_cluster.ramen.location }
output "lb_ip" { value = google_compute_global_address.lb.address }
output "buckets" { value = { for g, b in google_storage_bucket.group : g => b.url } }
output "console_service_account" { value = google_service_account.console.email }
output "worker_service_accounts" { value = { for k, sa in google_service_account.worker : k => sa.email } }
output "fernet_secret" { value = google_secret_manager_secret.fernet.id }
