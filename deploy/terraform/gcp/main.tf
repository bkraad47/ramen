# Ramen on GCP (CONTRACTS §7, v0.2.0): regional GKE Autopilot, Firestore Native, Artifact Registry,
# global static IP for the console, one groups bucket, console master GSA + Workload Identity.
# Nothing per-group is created here (the console's gcp adapter does that at runtime).
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
  groups_bucket = coalesce(var.groups_bucket, "ramen-${var.project}-groups")
  apis = [
    "container.googleapis.com", "firestore.googleapis.com", "secretmanager.googleapis.com",
    "storage.googleapis.com", "compute.googleapis.com", "artifactregistry.googleapis.com",
    "iam.googleapis.com", "logging.googleapis.com",
  ]
  console_roles = [
    "roles/storage.admin", "roles/secretmanager.admin", "roles/container.developer",
    "roles/logging.viewer", "roles/iam.serviceAccountAdmin", "roles/iam.serviceAccountUser",
    "roles/compute.securityAdmin", "roles/compute.loadBalancerAdmin",
    "roles/datastore.user",                  # Firestore state DB
    "roles/resourcemanager.projectIamAdmin", # bind conditioned roles to per-group GSAs (create_service_account)
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

resource "google_container_cluster" "ramen" {
  name                = var.name
  location            = var.region # regional Autopilot: nodes may land in any zone of the region
  enable_autopilot    = true
  deletion_protection = false
  release_channel { channel = "REGULAR" }
  ip_allocation_policy {}
  depends_on = [google_project_service.apis]
}

resource "google_firestore_database" "state" {
  name                    = "(default)"
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  delete_protection_state = "DELETE_PROTECTION_DISABLED"
  deletion_policy         = "DELETE"
  depends_on              = [google_project_service.apis]
}

resource "google_artifact_registry_repository" "ramen" {
  repository_id = "ramen"
  location      = var.region
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

resource "google_compute_global_address" "console" {
  name       = "ramen-console"
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "groups" {
  name                        = local.groups_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  depends_on                  = [google_project_service.apis]
}

resource "google_service_account" "console" {
  account_id   = "ramen-console"
  display_name = "Ramen console master SA"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "console" {
  for_each = toset(local.console_roles)
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.console.email}"
}

resource "google_service_account_iam_member" "console_wi" {
  service_account_id = google_service_account.console.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project}.svc.id.goog[ramen-system/console]"
  depends_on         = [google_container_cluster.ramen] # the WI pool exists only once the cluster does
}
