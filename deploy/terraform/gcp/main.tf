# Ramen on GCP — Phase 2 skeleton (v0.2.0). Regional GKE Autopilot, Firestore native, one GCS bucket
# per group, Secret Manager, global external HTTPS LB fronting per-zone worker NEGs.
# `terraform validate` clean; not yet applied to a real project.
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

locals {
  apis = ["container.googleapis.com", "firestore.googleapis.com", "secretmanager.googleapis.com", "compute.googleapis.com", "storage.googleapis.com", "logging.googleapis.com"]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

resource "google_container_cluster" "ramen" {
  name                = "${var.name}-gke"
  location            = var.region # regional = control plane + nodes across the region's zones
  enable_autopilot    = true
  deletion_protection = false
  release_channel { channel = "REGULAR" }
  ip_allocation_policy {}
  depends_on = [google_project_service.apis]
}

resource "google_firestore_database" "state" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.apis]
}

resource "google_storage_bucket" "group" {
  for_each                    = toset(var.groups)
  name                        = "${var.project}-ramen-${each.value}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  versioning { enabled = true }
}

resource "google_secret_manager_secret" "fernet" {
  secret_id = "${var.name}-fernet-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "fernet" {
  secret      = google_secret_manager_secret.fernet.id
  secret_data = var.fernet_key
}

# Per group+zone service accounts (F6.5); console master SA binds via Workload Identity.
resource "google_service_account" "console" {
  account_id   = "${var.name}-console"
  display_name = "Ramen console master SA"
}

resource "google_service_account" "worker" {
  for_each     = { for gz in setproduct(var.groups, var.zones) : "${gz[0]}-${gz[1]}" => { group = gz[0], zone = gz[1] } }
  account_id   = substr("ramen-${each.value.group}-${each.value.zone}", 0, 30)
  display_name = "Ramen worker ${each.value.group} @ ${each.value.zone}"
}

resource "google_storage_bucket_iam_member" "worker_read" {
  for_each = google_service_account.worker
  bucket   = google_storage_bucket.group[split("-", each.key)[0]].name
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${each.value.email}"
}

# Global external HTTPS LB. Worker NEGs are created by GKE from the Service annotation
# (deploy/helm: cloud.google.com/neg); their names are passed in via var.worker_negs.
resource "google_compute_global_address" "lb" {
  name = "${var.name}-lb-ip"
}

resource "google_compute_managed_ssl_certificate" "lb" {
  count = var.domain == "" ? 0 : 1
  name  = "${var.name}-cert"
  managed { domains = [var.domain] }
}

resource "google_compute_health_check" "worker" {
  name = "${var.name}-worker-hc"
  http_health_check {
    port         = 8080
    request_path = "/readyz"
  }
}

data "google_compute_network_endpoint_group" "worker" {
  for_each = var.worker_negs
  name     = each.value
  zone     = each.key
}

resource "google_compute_backend_service" "worker" {
  name                  = "${var.name}-worker-backend"
  protocol              = "HTTP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  health_checks         = [google_compute_health_check.worker.id]
  dynamic "backend" {
    for_each = data.google_compute_network_endpoint_group.worker
    content {
      group                 = backend.value.id
      balancing_mode        = "RATE"
      max_rate_per_endpoint = var.max_rate_per_endpoint
    }
  }
}

resource "google_compute_url_map" "lb" {
  name            = "${var.name}-urlmap"
  default_service = google_compute_backend_service.worker.id
}

resource "google_compute_target_https_proxy" "lb" {
  count            = var.domain == "" ? 0 : 1
  name             = "${var.name}-https-proxy"
  url_map          = google_compute_url_map.lb.id
  ssl_certificates = [google_compute_managed_ssl_certificate.lb[0].id]
}

resource "google_compute_global_forwarding_rule" "https" {
  count                 = var.domain == "" ? 0 : 1
  name                  = "${var.name}-https"
  target                = google_compute_target_https_proxy.lb[0].id
  ip_address            = google_compute_global_address.lb.address
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}
