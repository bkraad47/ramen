variable "subscription_id" {
  type        = string
  description = "Azure subscription the install lives in (az account show --query id -o tsv)."
}
variable "location" {
  type    = string
  default = "eastus"
}
variable "suffix" {
  type        = string
  default     = ""
  description = "Lowercase letters/digits appended to the globally unique names (storage account, Key Vault, ACR). Empty = random."
  validation {
    condition     = can(regex("^[a-z0-9]{0,10}$", var.suffix))
    error_message = "suffix: up to 10 lowercase letters or digits."
  }
}
variable "name" {
  type        = string
  default     = "ramen"
  description = "AKS cluster name; also the prefix of the resource group (ramen-<suffix>)."
}
variable "kubernetes_version" {
  type        = string
  default     = null
  description = "AKS version; null = the region's default."
}
variable "node_size" {
  type        = string
  default     = "Standard_B2s"
  description = "Cheap first: 2 vCPU / 4 GiB burstable."
}
variable "node_count" {
  type    = number
  default = 2
}
variable "node_max" {
  type    = number
  default = 3
}
variable "node_zones" {
  type        = list(string)
  default     = ["1"]
  description = "Availability zones of the node pool. Workers are pinned to their zone (topology.kubernetes.io/zone = <location>-<n>), so a Ramen zone `eastus-2` needs \"2\" here. One zone first."
}
variable "acme_email" {
  type        = string
  default     = ""
  description = "Contact for the Let's Encrypt account (cert-manager); empty = none."
}
variable "install_addons" {
  type        = bool
  default     = true
  description = "Install Envoy Gateway (Gateway API) and cert-manager from Terraform."
}
variable "envoy_gateway_chart_version" {
  type    = string
  default = "v1.9.2"
}
variable "cert_manager_chart_version" {
  type    = string
  default = "v1.21.2"
}
