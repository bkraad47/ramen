output "cluster_name" { value = google_container_cluster.satellite.name }
output "region" { value = var.region }
output "project" { value = var.project }
output "address" { value = google_compute_global_address.edge.address }
output "host" { value = local.public_hostname } # the satellite's `host` on Config → Clusters (Add satellite)
output "certificate_map" { value = google_certificate_manager_certificate_map.edge.name }
output "console_gsa" { value = local.console_gsa }
output "firewall_rule" { value = google_compute_firewall.home_to_satellite.name }
output "node_tag_command" { value = "gcloud compute firewall-rules list --project ${var.project} --filter=\"name~gke-${local.name}-\" --format='value(targetTags)' | head -1" }
output "kubeconfig_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.satellite.name} --region ${var.region} --project ${var.project}"
}
# Everything `helm install ramen-satellite` needs, ready to paste (deploy-gcp.md, satellite clusters)
output "helm_set" {
  value = join(" ", [
    "--set provider=gcp,project=${var.project},region=${var.region}",
    "--set console.identity=${local.console_gsa}",
    "--set gateway.staticIpName=${google_compute_global_address.edge.name},gateway.certificateMap=${google_certificate_manager_certificate_map.edge.name}",
  ])
}
output "node_service_account" { value = local.node_service_account }
