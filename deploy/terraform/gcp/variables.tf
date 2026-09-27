variable "project" { type = string }
variable "region" {
  type    = string
  default = "australia-southeast1"
}
variable "name" {
  type    = string
  default = "ramen"
}
variable "groups" {
  type        = list(string)
  default     = ["demo"]
  description = "Groups; one GCS bucket and one SA per group+zone."
}
variable "zones" {
  type        = list(string)
  default     = ["australia-southeast1-a"]
  description = "Zones workers run in (D6: one now, multi-zone ready)."
}
variable "worker_negs" {
  type        = map(string)
  default     = {}
  description = "zone => NEG name created by GKE for the worker Service (see helm NOTES)."
}
variable "domain" {
  type        = string
  default     = ""
  description = "Public hostname for the managed certificate; empty = no HTTPS frontend yet."
}
variable "fernet_key" {
  type      = string
  sensitive = true
  default   = "replace-me"
}
variable "max_rate_per_endpoint" {
  type    = number
  default = 100
}
