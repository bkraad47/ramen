# Ramen on GCP (CONTRACTS §7, v0.2.0; §11 security mediums, v0.3.1): regional GKE Autopilot, Firestore Native,
# Artifact Registry, global static IP for the console, one groups bucket, console master GSA + Workload Identity.
# Nothing per-group is created here (the console's gcp adapter does that at runtime, group image repositories too).
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
    "certificatemanager.googleapis.com", # Google-managed TLS for the console Gateway (D17 supersession, v0.5.5 I11)
  ]
  # D17 said a Google-managed cert waits "until a domain exists" because Google won't issue a publicly-trusted
  # cert for a bare IP. sslip.io resolves <ip-with-dashes>.sslip.io straight to that IP with zero DNS setup, so
  # the static IP alone is enough — no purchased domain required. `public_hostname` overrides it with a real one.
  public_hostname = coalesce(var.public_hostname, "${replace(google_compute_global_address.console.address, ".", "-")}.sslip.io")
  console_roles = concat([
    "roles/secretmanager.admin", # create ramen-<group>-* secrets and set their IAM (secret-level accessor bindings)
    "roles/container.developer", "roles/logging.viewer",
    "roles/iam.serviceAccountCreator", "roles/iam.serviceAccountDeleter", # per-group worker GSAs
    # §33.4a (D79): no roles/iam.serviceAccountUser. It carried actAs + getAccessToken on EVERY account of the project
    # (the default compute SA included), and IAM conditions cannot name service accounts (resource.name is not
    # provided for iam.googleapis.com), so "serviceAccountUser on ramen-* only" is not expressible. The console never
    # impersonates or signs as a worker GSA: Workload Identity is a setIamPolicy on the GSA (custom role below).
    "roles/compute.securityAdmin", "roles/compute.loadBalancerAdmin",
    "roles/datastore.user", # Firestore state DB
    ],
    # only with console_project_iam: bind project-wide roles (roles/logging.logWriter, ...) to worker GSAs
    var.console_project_iam ? ["roles/resourcemanager.projectIamAdmin"] : [],
    # §38.1: the judge proxy calls Vertex for a group's LLM rails; workers hold no AI role (unless one is approved)
    var.console_vertex ? ["roles/aiplatform.user"] : [],
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
  # §33.4a (D79): nodes run as the minimal node SA below, not the project's default compute SA (roles/editor).
  # Autopilot takes the node identity from the auto-provisioning defaults — at creation only: on a live cluster GKE
  # answers `400 Overriding Autopilot autoscaling settings is not allowed` (seen 2026-11-16 on ramen-test-261114), so
  # a cluster made before 1.0.0c keeps its account until re-created; set node_service_account = "default" for it.
  cluster_autoscaling {
    auto_provisioning_defaults {
      service_account = local.node_service_account
    }
  }
  depends_on = [google_project_service.apis, google_project_iam_member.nodes]
}

# §33.4a: the node identity — what GKE needs to write logs/metrics and pull images, nothing more. Overridable with
# var.node_service_account (a pre-1.0.0c cluster keeps "default", see above).
locals {
  node_service_account = coalesce(var.node_service_account, google_service_account.nodes.email)
  node_roles = [
    "roles/logging.logWriter", "roles/monitoring.metricWriter", "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
    "roles/autoscaling.metricsWriter", # the rest of roles/container.defaultNodeServiceAccount, split so each is visible
  ]
}

resource "google_service_account" "nodes" {
  account_id   = "ramen-nodes"
  display_name = "Ramen GKE node SA (least privilege)"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "nodes" {
  for_each = toset(local.node_roles)
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.nodes.email}"
}

# §38.2: the nodes pull the platform images and every group's repository (created at runtime), so the reader role is
# project-wide here; the nodes run every group's workers anyway.
resource "google_project_iam_member" "nodes_ar_read" {
  project = var.project
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

# S5 (sec-gaps): the project's default compute SA gets roles/editor when the Compute API is enabled. Nothing of Ramen
# runs as it on a new install (nodes: ramen-nodes@ above; satellites: the same SA), so the module removes that grant —
# a compromised console could otherwise set token-creator on the default SA (ramenConsoleSaIam carries project-wide
# setIamPolicy on service accounts) and act as a project editor. Only safe when nothing else in the project relies on
# the default SA (Compute Engine VMs, legacy Cloud Build/Functions/App Engine set up in the same project): turn
# var.remove_default_compute_editor off for a shared project. A cluster still running as the default SA (a pre-1.0.0c
# cluster, node_service_account = "default", or a satellite: default_compute_runs_nodes) first gets the node roles
# here, so its nodes keep logging and pulling images once editor is gone.
locals {
  default_compute_sa    = "${data.google_project.this.number}-compute@developer.gserviceaccount.com"
  default_sa_runs_nodes = var.node_service_account == "default" || var.node_service_account == local.default_compute_sa || var.default_compute_runs_nodes
  node_roles_all        = concat(local.node_roles, ["roles/artifactregistry.reader"])
}

resource "google_project_iam_member" "default_compute_node" {
  for_each = var.remove_default_compute_editor && local.default_sa_runs_nodes ? toset(local.node_roles_all) : toset([])
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${local.default_compute_sa}"
}

resource "google_project_iam_member_remove" "default_compute_editor" {
  count      = var.remove_default_compute_editor ? 1 : 0
  project    = var.project
  role       = "roles/editor"
  member     = "serviceAccount:${local.default_compute_sa}"
  depends_on = [google_project_service.apis, google_project_iam_member.default_compute_node]
}

resource "google_firestore_database" "state" {
  name                    = "(default)"
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  delete_protection_state = "DELETE_PROTECTION_DISABLED"
  deletion_policy         = "DELETE"
  depends_on              = [google_project_service.apis]
}

# The platform repository (§38.2): console, worker base and SkillSpector images. Group images live in their own
# repositories ramen-groups-<group> that the console creates; nothing that builds a group's image can write here.
resource "google_artifact_registry_repository" "ramen" {
  repository_id = "ramen"
  location      = var.region
  format        = "DOCKER"
  description   = "Ramen platform images; no build identity can write here (D85)"
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

# storage.admin on the groups bucket only (objects + bucket-level setIamPolicy for prefix-conditioned worker grants).
# §33.4a: the console binds each worker GSA's roles/storage.objectViewer on this bucket under the IAM condition
#   resource.name.startsWith("projects/_/buckets/<bucket>/objects/<group>/") || resource.name == "projects/_/buckets/<bucket>"
# (policy version 3; the bucket clause is objects.list, which GCS cannot scope to a prefix). Uniform bucket-level
# access above is what makes conditions possible. No project-level storage role is ever granted to a worker.
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

# §38.2 (D85, F2): no shared builder identity. Each group's build Job runs as KSA ramen-system/ramen-build-<group>,
# which the console makes the ONLY writer of that group's own repository `ramen-groups-<group>` (created by the
# console on the group's first build) — a Workload Identity Federation principal, no GSA. The `ramen` repository
# above is the platform repository (console, worker base, SkillSpector): no build principal can write it; the build
# KSAs may only read it (the worker base image is every build's FROM).
data "google_project" "this" {}

resource "google_artifact_registry_repository_iam_member" "builds_read_platform" {
  repository = google_artifact_registry_repository.ramen.name
  location   = google_artifact_registry_repository.ramen.location
  role       = "roles/artifactregistry.reader"
  member     = "principalSet://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project}.svc.id.goog/namespace/ramen-system"
  depends_on = [google_container_cluster.ramen] # the WI pool exists only once the cluster does
}

# The console creates, labels, binds and deletes the per-group repositories; it holds no upload/write permission, so it
# pushes nowhere itself. Artifact Registry does not evaluate IAM Conditions on resource.name (not among the services
# with resource attributes; a `ramen-groups-*` condition never matched on GKE, 1.0.0c), but every service evaluates
# resource TAGS. So (sec-gaps, closes the §38.2 GCP residual): a project tag key `ramen-repo` with the values
# `platform` (bound here to the platform repository) and `group` (bound by the console to each repository it creates);
# the console's repository rights are conditioned on `group`. A resource holds exactly one value per key, the console
# may attach only `group` (Tag User on that value alone) and holds no deleteTagBinding anywhere, so it can neither
# re-tag the platform repository nor reach it through the conditioned role. Create, list and createTagBinding are
# checked before a repository carries the tag (create/list on the location, the binding on the new repository) and
# stay unconditioned; none of them reads, writes or re-binds IAM on an existing repository.
resource "google_tags_tag_key" "repo" {
  parent      = "projects/${var.project}"
  short_name  = "ramen-repo"
  description = "Ramen image repositories: platform (no console rights) or group (console-managed), CONTRACTS §38.2"
  depends_on  = [google_project_service.apis]
}

resource "google_tags_tag_value" "platform" {
  parent      = google_tags_tag_key.repo.id
  short_name  = "platform"
  description = "the platform repository: console, worker base, SkillSpector"
}

resource "google_tags_tag_value" "group" {
  parent      = google_tags_tag_key.repo.id
  short_name  = "group"
  description = "a per-group repository ramen-groups-<group> (the console binds it at creation)"
}

# Regional resource → the location binding (regional Resource Manager endpoint). The parent uses the project number,
# the canonical form Resource Manager answers in (keeps `terraform plan` clean).
resource "google_tags_location_tag_binding" "platform_repo" {
  parent    = "//artifactregistry.googleapis.com/projects/${data.google_project.this.number}/locations/${google_artifact_registry_repository.ramen.location}/repositories/${google_artifact_registry_repository.ramen.repository_id}"
  tag_value = google_tags_tag_value.platform.id
  location  = google_artifact_registry_repository.ramen.location
}

resource "google_tags_tag_value_iam_member" "console_group_tag_user" {
  tag_value = google_tags_tag_value.group.name
  role      = "roles/resourcemanager.tagUser"
  member    = "serviceAccount:${google_service_account.console.email}"
}

resource "google_project_iam_custom_role" "console_build_create" {
  role_id     = "ramenConsoleBuildRepoCreate"
  title       = "Ramen console: create group image repositories"
  description = "create/list repositories and attach the ramen-repo tag to a new one (§38.2)"
  permissions = [
    "artifactregistry.repositories.create", "artifactregistry.repositories.list",
    "artifactregistry.repositories.createTagBinding", # with Tag User on ramen-repo/group only: no other value
  ]
  depends_on = [google_project_service.apis]
}

resource "google_project_iam_custom_role" "console_build_repos" {
  role_id     = "ramenConsoleBuildRepos"
  title       = "Ramen console: manage group image repositories"
  description = "get/update/delete and IAM on the per-group image repositories (ramen-repo=group, §38.2)"
  permissions = [
    "artifactregistry.repositories.get", "artifactregistry.repositories.update", "artifactregistry.repositories.delete",
    "artifactregistry.repositories.getIamPolicy", "artifactregistry.repositories.setIamPolicy",
  ]
  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "console_build_create" {
  project = var.project
  role    = google_project_iam_custom_role.console_build_create.id
  member  = "serviceAccount:${google_service_account.console.email}"
}

resource "google_project_iam_member" "console_build_repos" {
  project = var.project
  role    = google_project_iam_custom_role.console_build_repos.id
  member  = "serviceAccount:${google_service_account.console.email}"
  condition {
    title       = "ramen-repo-group"
    description = "only repositories tagged ramen-repo=group (§38.2)"
    expression  = "resource.matchTagId(\"${google_tags_tag_key.repo.id}\", \"${google_tags_tag_value.group.id}\")"
  }
}

# §33.4a (D79): the identity the evals and SkillSpector scan Jobs run as (KSA ramen-system/ramen-evals). They execute
# a group repo's promptfoo config, so this GSA holds logging only: no registry, no bucket, no Secret Manager.
resource "google_service_account" "evals" {
  account_id   = "ramen-evals"
  display_name = "Ramen evals/scan Jobs"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "evals_logs" {
  project = var.project
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.evals.email}"
}

resource "google_service_account_iam_member" "evals_wi" {
  service_account_id = google_service_account.evals.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project}.svc.id.goog[ramen-system/ramen-evals]"
  depends_on         = [google_container_cluster.ramen]
}

# Google-managed TLS for the Gateway (D17 supersession, §gateway.yaml `networking.gke.io/certmap`): a
# publicly-trusted cert for local.public_hostname, so an MCP bridge (or any client) trusts it with the
# system CA store — no kubectl secret pull, no --tls custom-CA flag. Validated by the LB it's attached to
# (load-balancer authorization), not by proving domain ownership, so it needs no DNS TXT record.
resource "google_certificate_manager_certificate" "console" {
  name = "ramen-console"
  managed { domains = [local.public_hostname] }
  depends_on = [google_project_service.apis]
}

resource "google_certificate_manager_certificate_map" "console" {
  name       = "ramen-console"
  depends_on = [google_project_service.apis]
}

resource "google_certificate_manager_certificate_map_entry" "console" {
  name         = "ramen-console"
  map          = google_certificate_manager_certificate_map.console.name
  hostname     = local.public_hostname
  certificates = [google_certificate_manager_certificate.console.id]
}
