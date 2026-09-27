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
