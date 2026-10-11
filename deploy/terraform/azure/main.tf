# Ramen on Azure (CONTRACTS §21.6, v0.7.5, D54): AKS (free tier, OIDC issuer + Workload Identity), ACR, one storage
# account (blob container `groups` = the code bucket, table `ramen` = the console store), Key Vault (RBAC) for secrets,
# a static public IP for the Envoy Gateway edge, and the console's user-assigned identity. Nothing per-group is created
# here: the console's azure adapter makes each zone's identity, role assignments, namespace and routes at runtime.
# Every role the console holds is scoped to this resource group, never the subscription.
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
    helm    = { source = "hashicorp/helm", version = "~> 2.16" }
  }
}

provider "azurerm" {
  subscription_id = var.subscription_id
  features {
    key_vault {
      purge_soft_delete_on_destroy    = true # a throwaway install leaves no soft-deleted vault behind
      recover_soft_deleted_key_vaults = false
    }
    resource_group {
      prevent_deletion_if_contains_resources = false # the AKS-managed node resource group is deleted with the cluster
    }
  }
}

data "azurerm_client_config" "me" {}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

locals {
  suffix          = coalesce(var.suffix, random_string.suffix.result)
  public_hostname = "${replace(azurerm_public_ip.edge.ip_address, ".", "-")}.sslip.io"
  # what the console may do, all at resource-group scope (SEC-08 spirit: nothing subscription-wide)
  console_roles = [
    "Storage Blob Data Contributor",  # sync group repos into the `groups` container
    "Storage Table Data Contributor", # the console store (RAMEN_STORE=azuretables)
    "Key Vault Secrets Officer",      # create/read/delete ramen--<group>--<VAR> secrets
    "Managed Identity Contributor",   # per-zone identities + their federated credentials
    # per-zone role assignments (blob reader, per-secret Key Vault Secrets User, approved permission requests);
    # the console only ever assigns the roles of its permission catalogue, and only inside this resource group
    "Role Based Access Control Administrator",
  ]
}

resource "azurerm_resource_group" "ramen" {
  name     = "${var.name}-${local.suffix}"
  location = var.location
}

resource "azurerm_kubernetes_cluster" "ramen" {
  name                      = var.name
  location                  = azurerm_resource_group.ramen.location
  resource_group_name       = azurerm_resource_group.ramen.name
  dns_prefix                = "${var.name}-${local.suffix}"
  kubernetes_version        = var.kubernetes_version
  sku_tier                  = "Free"
  oidc_issuer_enabled       = true
  workload_identity_enabled = true
  default_node_pool {
    name                 = "system"
    vm_size              = var.node_size
    node_count           = var.node_count
    auto_scaling_enabled = true
    min_count            = 1
    max_count            = var.node_max
    zones                = var.node_zones
    upgrade_settings { max_surge = "10%" }
  }
  identity { type = "SystemAssigned" }
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    load_balancer_sku   = "standard"
  }
}

resource "azurerm_container_registry" "ramen" {
  name                = "ramen${local.suffix}"
  resource_group_name = azurerm_resource_group.ramen.name
  location            = azurerm_resource_group.ramen.location
  sku                 = "Basic"
}

resource "azurerm_role_assignment" "kubelet_acr_pull" {
  scope                = azurerm_container_registry.ramen.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.ramen.kubelet_identity[0].object_id
}

resource "azurerm_storage_account" "ramen" {
  name                            = "ramen${local.suffix}"
  resource_group_name             = azurerm_resource_group.ramen.name
  location                        = azurerm_resource_group.ramen.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = true # Terraform's data-plane calls below; workloads use Entra (workload identity) only
}

resource "azurerm_storage_container" "groups" {
  name                  = "groups"
  storage_account_id    = azurerm_storage_account.ramen.id
  container_access_type = "private"
}

resource "azurerm_storage_table" "state" {
  name                 = "ramen"
  storage_account_name = azurerm_storage_account.ramen.name
}

resource "azurerm_key_vault" "ramen" {
  name                       = "ramen-${local.suffix}"
  location                   = azurerm_resource_group.ramen.location
  resource_group_name        = azurerm_resource_group.ramen.name
  tenant_id                  = data.azurerm_client_config.me.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = false
  soft_delete_retention_days = 7
}

# The edge's address. In the node resource group, so the AKS cloud provider may bind it to the Envoy Service with no
# extra role; its DNS label gives a stable <label>.<location>.cloudapp.azure.com next to the sslip.io name.
resource "azurerm_public_ip" "edge" {
  name                = "ramen-edge"
  resource_group_name = azurerm_kubernetes_cluster.ramen.node_resource_group
  location            = azurerm_resource_group.ramen.location
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"]
  domain_name_label   = "${var.name}-${local.suffix}"
}

resource "azurerm_user_assigned_identity" "console" {
  name                = "ramen-console"
  resource_group_name = azurerm_resource_group.ramen.name
  location            = azurerm_resource_group.ramen.location
}

resource "azurerm_federated_identity_credential" "console" {
  name      = "ramen-console"
  parent_id = azurerm_user_assigned_identity.console.id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = azurerm_kubernetes_cluster.ramen.oidc_issuer_url
  subject   = "system:serviceaccount:ramen-system:console"
}

resource "azurerm_role_assignment" "console" {
  for_each             = toset(local.console_roles)
  scope                = azurerm_resource_group.ramen.id
  role_definition_name = each.value
  principal_id         = azurerm_user_assigned_identity.console.principal_id
}
