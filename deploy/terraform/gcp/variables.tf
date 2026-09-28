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
