variable "region" {
  type    = string
  default = "us-east-1"
}
variable "name" {
  type        = string
  default     = "ramen"
  description = "EKS cluster name (also the node group, VPC Name tag and Container Insights log group)."
}
variable "kubernetes_version" {
  type    = string
  default = "1.35"
}
variable "node_instance_type" {
  type        = string
  default     = "t3.small"
  description = "Cheap first: t3.small. Workers are pinned to their AZ, so the node count is per AZ x 2 subnets (see node_count)."
}
variable "node_count" {
  type    = number
  default = 4
}
variable "node_max" {
  type    = number
  default = 5
}
variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}
variable "table" {
  type        = string
  default     = "ramen"
  description = "DynamoDB state table (RAMEN_DDB_TABLE)."
}
variable "groups_bucket" {
  type        = string
  default     = ""
  description = "S3 bucket holding every group's synced repo under <bucket>/<group>/. Empty = ramen-<account>-groups."
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
  description = "ALB IngressGroup name (helm value aws.albGroup / RAMEN_ALB_GROUP): scopes the console's WAF and ALB permissions."
}
