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
  default     = "Standard_B2s_v2"
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
# 1.0.0c: Azure Managed Redis as the chart's shared Redis (`redis.externalUrl`); off = bring your own / `redis.enabled`
variable "managed_redis" {
  type        = bool
  default     = true
  description = "Create an Azure Managed Redis and hand its rediss:// URL to the chart (helm_set gains redis.externalUrl)."
}
variable "managed_redis_sku" {
  type        = string
  default     = "Balanced_B0"
  description = "Managed Redis SKU; Balanced_B0 is the smallest (about USD 0.02/h without HA)."
}
variable "managed_redis_ha" {
  type        = bool
  default     = false
  description = "High availability (two nodes). Off for a test install."
}
