# Ramen on GCP (CONTRACTS §7, v0.2.0; §11 security mediums, v0.3.1): regional GKE Autopilot, Firestore Native,
# Artifact Registry, global static IP for the console, one groups bucket, console master GSA + Workload Identity.
# Nothing per-group is created here (the console's gcp adapter does that at runtime).
# SEC-08: the console GSA holds no project-level setIamPolicy. Bucket roles for worker GSAs are bound on the groups
# bucket, secret roles on each `ramen-<group>-*` secret, Workload Identity on the worker GSA itself. Project-wide
# roles from approved permission requests (logging, monitoring, ...) need `console_project_iam = true`.
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
    # the console's project-wide role bindings (console_project_iam) go through Resource Manager; without the API
    # the call is a bare 403 that looks like a missing role — found on the 0.5.1 GKE run
    "cloudresourcemanager.googleapis.com",
  ]
  console_roles = concat([
    "roles/secretmanager.admin", # create ramen-<group>-* secrets and set their IAM (secret-level accessor bindings)
    "roles/container.developer", "roles/logging.viewer",
    "roles/iam.serviceAccountCreator", "roles/iam.serviceAccountDeleter", # per-group worker GSAs
    "roles/iam.serviceAccountUser",
    "roles/compute.securityAdmin", "roles/compute.loadBalancerAdmin",
    "roles/datastore.user", # Firestore state DB
    ],
    # only with console_project_iam: bind project-wide roles (roles/logging.logWriter, ...) to worker GSAs
    var.console_project_iam ? ["roles/resourcemanager.projectIamAdmin"] : [],
  )
}

# Custom role: IAM on service accounts (Workload Identity bindings) + project number lookup. IAM conditions cannot
# match service accounts by name (resource names carry the unique id), so the limit to ramen-* accounts is enforced
# by the console (it only ever addresses `ramen-<group>-<zone>@`), not by IAM.
resource "google_project_iam_custom_role" "console_sa_iam" {
  role_id     = "ramenConsoleSaIam"
  title       = "Ramen console service-account IAM"
  description = "getIamPolicy/setIamPolicy on service accounts + projects.get"
  permissions = [
    "iam.serviceAccounts.getIamPolicy", "iam.serviceAccounts.setIamPolicy",
    "iam.serviceAccounts.get", "iam.serviceAccounts.list",
    "resourcemanager.projects.get",
  ]
  depends_on = [google_project_service.apis]
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

resource "google_project_iam_member" "console_sa_iam" {
  project = var.project
  role    = google_project_iam_custom_role.console_sa_iam.id
  member  = "serviceAccount:${google_service_account.console.email}"
}

# storage.admin on the groups bucket only (objects + bucket-level setIamPolicy for prefix-conditioned worker grants)
resource "google_storage_bucket_iam_member" "console_groups" {
  bucket = google_storage_bucket.groups.name
  role   = "roles/storage.admin"
  member = "serviceAccount:${google_service_account.console.email}"
}

resource "google_service_account_iam_member" "console_wi" {
  service_account_id = google_service_account.console.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project}.svc.id.goog[ramen-system/console]"
  depends_on         = [google_container_cluster.ramen] # the WI pool exists only once the cluster does
}
