output "cluster_name" { value = google_container_cluster.ramen.name }
output "region" { value = var.region }
output "console_ip" { value = google_compute_global_address.console.address }
output "public_hostname" { value = local.public_hostname }
output "certificate_map" { value = google_certificate_manager_certificate_map.console.name }
output "artifact_repo" { value = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.ramen.repository_id}" }
output "console_gsa" { value = google_service_account.console.email }
output "groups_bucket" { value = google_storage_bucket.groups.name }
output "project" { value = var.project }
output "builder_set" { value = "--set builder.enabled=true" }     # §38.2: per-group build identities, no shared builder
output "evals_gsa" { value = google_service_account.evals.email } # helm: --set evals.gsa=<this> (§33.4a)
output "node_service_account" { value = local.node_service_account }
# §34.1 optional engines, ready to paste into `helm install ramen` (docs/wiki/deploy-*.md)
output "engines_set" { value = local.engines_set }
locals {
  engines_set = "--set otel.collector=${var.otel_collector},evals.engine=${var.evals_engine},skillspector.engine=${var.skillspector_engine}"
}
# §38.2: the tag value the console binds to each group repository (its default, `<project>/ramen-repo/group`;
# chart console.env.RAMEN_AR_GROUP_TAG overrides it)
output "ar_group_tag" { value = "${var.project}/${google_tags_tag_key.repo.short_name}/${google_tags_tag_value.group.short_name}" }
