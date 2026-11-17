# Ramen satellite cluster on GCP (CONTRACTS §28.3, v1.0.0b): a worker-only regional GKE Autopilot cluster in another
# region of the SAME project as the home apply (deploy/terraform/gcp), with its own global static IP and
# Google-managed certificate. The console in the home cluster drives it directly (console GSA → GKE API).
# Nothing else is created here: Firestore, the groups bucket, Artifact Registry and the console GSA are all
# shared from the home apply. APIs are re-declared so this module can be applied on its own; disable_on_destroy is
# off so destroying a satellite never switches an API off under the home cluster.
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
  name = coalesce(var.name, "ramen-${var.region}")
  apis = [
    "container.googleapis.com", "firestore.googleapis.com", "secretmanager.googleapis.com",
    "storage.googleapis.com", "compute.googleapis.com", "artifactregistry.googleapis.com",
    "iam.googleapis.com", "logging.googleapis.com", "cloudresourcemanager.googleapis.com",
    "certificatemanager.googleapis.com",
  ]
  # sslip.io hostname of the static IP by default (see the home module); `public_hostname` overrides it.
  public_hostname = coalesce(var.public_hostname, "${replace(google_compute_global_address.edge.address, ".", "-")}.sslip.io")
  console_gsa     = coalesce(var.console_gsa, "ramen-console@${var.project}.iam.gserviceaccount.com")
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

resource "google_container_cluster" "satellite" {
  name                = local.name
  location            = var.region # regional Autopilot: nodes may land in any zone of the region
  enable_autopilot    = true
  deletion_protection = false
  release_channel { channel = "REGULAR" }
  ip_allocation_policy {}
  depends_on = [google_project_service.apis]
}

# §28.1: the console in the home cluster calls satellite workers by pod IP (smoke, metrics, reload, canary). Both
# clusters are VPC-native on the default network, so the pod ranges route; this lets the home pods in on 8080.
# Name matches the hand-made rule of the first proof, so it imports: terraform import google_compute_firewall.home_to_satellite <name>
resource "google_compute_firewall" "home_to_satellite" {
  name          = "ramen-home-to-satellite-${var.region}"
  network       = "default"
  direction     = "INGRESS"
  source_ranges = [var.home_pod_cidr]
  target_tags   = var.node_tag != "" ? [var.node_tag] : null
  allow {
    protocol = "tcp"
    ports    = ["8080"]
  }
  depends_on = [google_project_service.apis]
}

# The satellite edge: its own global external HTTPS LB (GKE Gateway in deploy/helm/ramen-satellite) on this address.
resource "google_compute_global_address" "edge" {
  name       = "ramen-${var.region}"
  depends_on = [google_project_service.apis]
}

resource "google_certificate_manager_certificate" "edge" {
  name = "ramen-${var.region}"
  managed { domains = [local.public_hostname] }
  depends_on = [google_project_service.apis]
}

resource "google_certificate_manager_certificate_map" "edge" {
  name       = "ramen-${var.region}"
  depends_on = [google_project_service.apis]
}

resource "google_certificate_manager_certificate_map_entry" "edge" {
  name         = "ramen-${var.region}"
  map          = google_certificate_manager_certificate_map.edge.name
  hostname     = local.public_hostname
  certificates = [google_certificate_manager_certificate.edge.id]
}
