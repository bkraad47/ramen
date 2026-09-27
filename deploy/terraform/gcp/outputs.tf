output "cluster_name" { value = google_container_cluster.ramen.name }
output "region" { value = var.region }
output "console_ip" { value = google_compute_global_address.console.address }
output "artifact_repo" { value = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.ramen.repository_id}" }
output "console_gsa" { value = google_service_account.console.email }
output "groups_bucket" { value = google_storage_bucket.groups.name }
output "project" { value = var.project }
