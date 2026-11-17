variable "region" {
  type        = string
  description = "The satellite's region, different from the home cluster's (e.g. eu-west-1)."
}
variable "name" {
  type        = string
  default     = ""
  description = "EKS cluster name (also the node group, VPC Name tag, IAM role prefix and log group). Empty = ramen-<region>."
}
variable "console_role_arn" {
  type        = string
  default     = ""
  description = "The console pod role (home apply output console_role_arn), given cluster access through an EKS access entry. Empty = arn:aws:iam::<account>:role/ramen/ramen-console."
}
variable "home_region" {
  type        = string
  default     = "us-east-1"
  description = "The home cluster's region (peering accepter side)."
}
variable "home_vpc_id" {
  type        = string
  description = "The home VPC (home apply output vpc_id). §28.1: the console addresses satellite workers by pod IP, so the two VPCs are peered."
}
variable "home_vpc_cidr" {
  type        = string
  default     = "10.42.0.0/16"
  description = "The home VPC's CIDR (home apply output vpc_cidr); routed over the peering and allowed in on 8080. Must not overlap vpc_cidr."
}
variable "home_route_table_ids" {
  type        = list(string)
  default     = []
  description = "Route tables of the home VPC that get the route to this VPC (home apply output route_table_ids). Empty = every route table of home_vpc_id."
}
variable "console_group" {
  type        = string
  default     = "ramen-console"
  description = "Kubernetes group the access entry maps the console role to; the satellite chart binds the console ClusterRoles to it (helm value aws.consoleGroup)."
}
variable "kubernetes_version" {
  type    = string
  default = "1.35"
}
variable "node_instance_type" {
  type    = string
  default = "t3.small"
}
variable "node_count" {
  type    = number
  default = 2
}
variable "node_max" {
  type    = number
  default = 4
}
variable "vpc_cidr" {
  type    = string
  default = "10.43.0.0/16"
}
variable "log_retention_days" {
  type    = number
  default = 14
}
variable "install_addons" {
  type        = bool
  default     = true
  description = "Install the AWS Load Balancer Controller and Fluent Bit via Helm from Terraform (needs aws CLI + kubectl auth)."
}
variable "alb_controller_chart_version" {
  type    = string
  default = "1.13.0"
}
variable "fluent_bit_chart_version" {
  type    = string
  default = "0.1.35"
}
variable "alb_group" {
  type        = string
  default     = "ramen"
  description = "ALB IngressGroup name on this cluster (helm value aws.albGroup); the console's WAF web ACL in this region is named after it."
}
