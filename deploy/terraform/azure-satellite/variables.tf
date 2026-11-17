variable "subscription_id" {
  type        = string
  description = "The home apply's subscription (az account show --query id -o tsv)."
}
variable "location" {
  type        = string
  description = "The satellite's location, different from the home cluster's (e.g. westeurope)."
}
variable "console_principal_id" {
  type        = string
  description = "Object (principal) id of the console's user-assigned identity: az identity show -g <home rg> -n ramen-console --query principalId -o tsv. Also the chart's console.identity."
}
variable "home_vnet_id" {
  type        = string
  description = "The home cluster's VNet id (home apply output vnet_id). §28.1: the console addresses satellite workers by pod IP, so the two VNets are peered."
}
variable "home_vnet_name" {
  type        = string
  description = "The home VNet's name (home apply output vnet_name; aks-vnet-<n> in the node resource group)."
}
variable "home_vnet_resource_group" {
  type        = string
  description = "The home VNet's resource group (home apply output vnet_resource_group: the home node resource group MC_...)."
}
variable "vnet_cidr" {
  type        = string
  default     = "10.2.0.0/16"
  description = "The satellite VNet; nodes and pods (Azure CNI, no overlay, so pod IPs route over the peering) take addresses from it. Must not overlap the home VNet (AKS default 10.224.0.0/12) or its service range (10.0.0.0/16)."
}
variable "acr_id" {
  type        = string
  default     = ""
  description = "Resource id of the home ACR (az acr show -n <acr> --query id -o tsv): the satellite's kubelet gets AcrPull on it. Empty = no assignment (public images only)."
}
variable "resource_group" {
  type        = string
  default     = ""
  description = "An existing resource group to put the cluster in. Empty = create ramen-<suffix>-<location>."
}
variable "suffix" {
  type        = string
  default     = ""
  description = "Lowercase letters/digits in the resource group, DNS prefix and public IP label. Empty = random."
  validation {
    condition     = can(regex("^[a-z0-9]{0,10}$", var.suffix))
    error_message = "suffix: up to 10 lowercase letters or digits."
  }
}
variable "name" {
  type        = string
  default     = "ramen"
  description = "AKS cluster name (unique within its resource group)."
}
variable "kubernetes_version" {
  type    = string
  default = null
}
variable "node_size" {
  type    = string
  default = "Standard_B2s_v2"
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
  description = "Availability zones of the node pool; a Ramen zone <location>-<n> needs \"<n>\" here. [] where AKS offers none."
}
variable "install_addons" {
  type    = bool
  default = true
}
variable "envoy_gateway_chart_version" {
  type    = string
  default = "v1.9.2"
}
variable "cert_manager_chart_version" {
  type    = string
  default = "v1.21.2"
}
