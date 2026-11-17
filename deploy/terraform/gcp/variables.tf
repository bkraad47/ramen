variable "project" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "name" {
  type        = string
  default     = "ramen"
  description = "GKE Autopilot cluster name."
}
variable "groups_bucket" {
  type        = string
  default     = ""
  description = "GCS bucket holding every group's synced repo under <bucket>/<group>/. Empty = ramen-<project>-groups."
}
variable "console_project_iam" {
  type        = bool
  default     = false
  description = "Grant the console GSA roles/resourcemanager.projectIamAdmin so approved permission requests can bind project-wide roles (logging.logWriter, monitoring.metricWriter, ...) to worker GSAs. Off by default (SEC-08); bucket/secret roles never need it."
}
variable "node_service_account" {
  type        = string
  default     = ""
  description = "Service account the GKE nodes run as. Empty = the module's minimal ramen-nodes@ SA (§33.4a), honoured at cluster creation. GKE refuses to change it on an existing Autopilot cluster, so a cluster created before 1.0.0c sets \"default\" (the project's default compute SA, what it already runs as) until it is re-created; see deploy-gcp.md."
}
variable "remove_default_compute_editor" {
  type        = bool
  default     = true
  description = "S5: remove roles/editor from the project's default compute SA (<number>-compute@developer.gserviceaccount.com). Ramen's nodes run as ramen-nodes@; a cluster still on the default SA gets the node roles first. Only safe when nothing else in the project relies on the default SA's editor grant (Compute Engine VMs, legacy Cloud Build/Functions/App Engine): set false in a shared project. Destroying the module does not give editor back."
}
variable "default_compute_runs_nodes" {
  type        = bool
  default     = false
  description = "Set true while a satellite cluster of this project (deploy/terraform/gcp-satellite with node_service_account = \"default\") still runs as the default compute SA: it then keeps the node roles when editor is removed. The home cluster's own \"default\" is detected from node_service_account."
}
variable "public_hostname" {
  type        = string
  default     = ""
  description = "Hostname for the console Gateway's Google-managed cert. Empty = auto-derive a free sslip.io hostname from the static IP (v0.5.5 I11); set a real domain here once one exists (DNS A record -> the console_ip output)."
}
# §34.1 optional engines: chart values otel.collector / evals.engine / skillspector.engine, pasted into `helm install`
# through `terraform output -raw engines_set`. Off by default; turning one on changes only that engine's objects.
variable "otel_collector" {
  type        = string
  default     = "none"
  description = "none | jaeger | phoenix: the chart's try-out trace collector in ramen-system (§24.3)."
  validation {
    condition     = contains(["none", "jaeger", "phoenix"], var.otel_collector)
    error_message = "otel_collector: none, jaeger or phoenix."
  }
}
variable "evals_engine" {
  type        = string
  default     = "none"
  description = "none | promptfoo: the promptfoo server the evals Jobs share their runs with (§29.2)."
  validation {
    condition     = contains(["none", "promptfoo"], var.evals_engine)
    error_message = "evals_engine: none or promptfoo."
  }
}
variable "skillspector_engine" {
  type        = string
  default     = "none"
  description = "none | skillspector: scan every group's skills as a Job after the evals (§30)."
  validation {
    condition     = contains(["none", "skillspector"], var.skillspector_engine)
    error_message = "skillspector_engine: none or skillspector."
  }
}
variable "console_vertex" {
  type        = bool
  default     = true
  description = "Grant the console GSA roles/aiplatform.user: Vertex-backed LLM rails and evals go through the console's judge proxy (§38.1), workers never call a provider."
}
