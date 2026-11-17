variable "project" {
  type        = string
  description = "The home apply's project id (deploy/terraform/gcp var.project): satellites share its store, bucket, registry and GSAs."
}
variable "region" {
  type        = string
  description = "The satellite's region, different from the home cluster's (e.g. australia-southeast1)."
}
variable "name" {
  type        = string
  default     = ""
  description = "GKE Autopilot cluster name. Empty = ramen-<region>."
}
variable "public_hostname" {
  type        = string
  default     = ""
  description = "Hostname for the satellite Gateway's Google-managed cert. Empty = <ip-with-dashes>.sslip.io of the static IP; set a real domain here once one exists (DNS A record -> the address output)."
}
variable "home_pod_cidr" {
  type        = string
  description = "The home cluster's pod range (gcloud container clusters describe ramen --region <home region> --format='value(clusterIpv4Cidr)', e.g. 10.7.128.0/17). The console addresses satellite workers by pod IP (§28.1): both clusters share the default VPC, this opens tcp:8080 from the home pods to the satellite nodes."
}
variable "node_tag" {
  type        = string
  default     = ""
  description = "The satellite nodes' network tag, gke-<name>-<hash>-node, to scope the firewall rule to them. GKE does not expose the hash before the cluster exists: apply once, read it with gcloud compute firewall-rules list --filter=\"name~gke-<name>\" --format='value(targetTags)', set it and apply again. Empty = the rule targets every instance on the default network (8080 from the home pod range only)."
}
variable "console_gsa" {
  type        = string
  default     = ""
  description = "The console's GSA email (home apply output console_gsa), bound as a cluster user by the satellite chart. Empty = ramen-console@<project>.iam.gserviceaccount.com. Output only; no IAM is created here."
}
variable "node_service_account" {
  type        = string
  default     = ""
  description = "Service account the satellite's nodes run as. Empty = ramen-nodes@<project> (made by the home apply). A satellite created before this change runs as the default compute SA and GKE cannot change it in place: set \"default\" here and default_compute_runs_nodes = true in the home apply (it keeps the node roles when roles/editor is removed) until the satellite is re-created. No IAM is created here."
}
