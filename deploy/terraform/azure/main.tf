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
  # S6b: without a policy engine AKS stores NetworkPolicies and enforces none. Calico, because it is the engine both
  # Ramen AKS shapes take: this overlay cluster and the satellite's node-subnet Azure CNI, where azurerm accepts Cilium
  # only with overlay or a pod subnet (Azure NPM is the legacy engine). Setting it on an existing cluster may force
  # Terraform to replace the cluster (read the plan; deploy-azure.md, upgrading) — new clusters are unaffected.
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "calico"
    load_balancer_sku   = "standard"
  }
}

# §38.2 (D85): repository permissions (ABAC). Role assignments on the registry may carry a condition naming a
# repository, which is how a group's build identity can push to ramen/groups/<group>/worker and nowhere else. In this
# mode ACR honours the "Container Registry Repository *" roles, not AcrPull/AcrPush, hence the kubelet's role below.
# The platform repositories ramen/{console,worker,skillspector} are written by your own pushes (az acr login), never
# by a build identity.
resource "azurerm_container_registry" "ramen" {
  name                 = "ramen${local.suffix}"
  resource_group_name  = azurerm_resource_group.ramen.name
  location             = azurerm_resource_group.ramen.location
  sku                  = "Basic"
  role_assignment_mode = "AbacRepositoryPermissions"
}

# Your own pushes of the platform images (az acr login + docker/crane push): in ABAC mode Owner/Contributor grant no
# data-plane rights, so the principal that runs terraform gets "Container Registry Repository Writer" on the platform
# repositories only (1.0.0c: a fresh registry answered 401 to the operator's push). Group repositories stay writable
# only by their own build identity.
resource "azurerm_role_assignment" "operator_acr_push" {
  scope                = azurerm_container_registry.ramen.id
  role_definition_name = "Container Registry Repository Writer"
  principal_id         = data.azurerm_client_config.me.object_id
  condition_version    = "2.0"
  condition            = <<-EOT
    (
     (
      !(ActionMatches{'Microsoft.ContainerRegistry/registries/repositories/content/read'})
      AND !(ActionMatches{'Microsoft.ContainerRegistry/registries/repositories/content/write'})
      AND !(ActionMatches{'Microsoft.ContainerRegistry/registries/repositories/metadata/read'})
      AND !(ActionMatches{'Microsoft.ContainerRegistry/registries/repositories/metadata/write'})
     )
     OR
     (
      @Request[Microsoft.ContainerRegistry/registries/repositories:name] StringStartsWithIgnoreCase 'ramen/console'
      OR @Request[Microsoft.ContainerRegistry/registries/repositories:name] StringStartsWithIgnoreCase 'ramen/worker'
      OR @Request[Microsoft.ContainerRegistry/registries/repositories:name] StringStartsWithIgnoreCase 'ramen/skillspector'
     )
    )
  EOT
}

resource "azurerm_role_assignment" "kubelet_acr_pull" {
  scope                = azurerm_container_registry.ramen.id
  role_definition_name = "Container Registry Repository Reader" # every repository: platform and every group's
  principal_id         = azurerm_kubernetes_cluster.ramen.kubelet_identity[0].object_id
}

resource "azurerm_storage_account" "ramen" {
  name                            = "ramen${local.suffix}"
  resource_group_name             = azurerm_resource_group.ramen.name
  location                        = azurerm_resource_group.ramen.location
  account_tier                    = "Standard"
  account_kind                    = "StorageV2" # the default, written down: ABAC conditions on blob roles need GPv2 (§33.4a)
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
  zones               = length(var.node_zones) > 0 ? ["1", "2", "3"] : null
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

# §38.2 (D85, F2): no shared builder identity. The console creates per group the managed identity ramen-build-<group>
# (federated with KSA ramen-system/ramen-build-<group>) and assigns it "Container Registry Repository Writer" on this
# registry under an ABAC condition naming ramen/groups/<group>/worker only (plus "... Reader" on the worker base
# repository). Its "Managed Identity Contributor" and "Role Based Access Control Administrator" above cover that.
# To remove a deleted group's repository it holds this data-plane role, conditioned to ramen/groups/* and without any
# write action: it can delete group images, never push anything.
resource "azurerm_role_definition" "console_acr_cleaner" {
  name        = "Ramen group repository cleaner (${local.suffix})"
  scope       = azurerm_container_registry.ramen.id
  description = "Read and delete ACR repositories (no push); assigned with a ramen/groups/ condition (D85)"
  permissions {
    data_actions = [
      "Microsoft.ContainerRegistry/registries/repositories/metadata/read",
      "Microsoft.ContainerRegistry/registries/repositories/metadata/delete",
      "Microsoft.ContainerRegistry/registries/repositories/content/delete",
    ]
  }
  assignable_scopes = [azurerm_container_registry.ramen.id]
}

resource "azurerm_role_assignment" "console_acr_cleaner" {
  scope              = azurerm_container_registry.ramen.id
  role_definition_id = azurerm_role_definition.console_acr_cleaner.role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.console.principal_id
  condition_version  = "2.0"
  condition          = <<-EOT
    (
     (
      !(ActionMatches{'Microsoft.ContainerRegistry/registries/repositories/metadata/read'})
      AND
      !(ActionMatches{'Microsoft.ContainerRegistry/registries/repositories/metadata/delete'})
      AND
      !(ActionMatches{'Microsoft.ContainerRegistry/registries/repositories/content/delete'})
     )
     OR
     (
      @Request[Microsoft.ContainerRegistry/registries/repositories:name] StringStartsWithIgnoreCase 'ramen/groups/'
     )
    )
  EOT
}

# §33.4a (D79): the identity the evals and SkillSpector scan Jobs run as (KSA ramen-system/ramen-evals). They execute
# a group repo's promptfoo config, so it holds NO role assignment: no registry, no storage, no Key Vault. (Pod logs
# reach Azure Monitor through the node agent, not through this identity.)
resource "azurerm_user_assigned_identity" "evals" {
  name                = "ramen-evals"
  resource_group_name = azurerm_resource_group.ramen.name
  location            = azurerm_resource_group.ramen.location
}

resource "azurerm_federated_identity_credential" "evals" {
  name      = "ramen-evals"
  parent_id = azurerm_user_assigned_identity.evals.id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = azurerm_kubernetes_cluster.ramen.oidc_issuer_url
  subject   = "system:serviceaccount:ramen-system:ramen-evals"
}

# Azure Managed Redis (1.0.0c): the group throttle / task store the chart hands every zone as RAMEN_REDIS_URL
# (`redis.externalUrl`), in place of the in-cluster Redis. TLS-only on port 10000 (the node and the workers speak
# `rediss://`); access-key auth; public endpoint — AKS egress reaches it over the internet, the key is the gate.
resource "azurerm_managed_redis" "ramen" {
  count                     = var.managed_redis ? 1 : 0
  name                      = "${var.name}-${local.suffix}"
  resource_group_name       = azurerm_resource_group.ramen.name
  location                  = azurerm_resource_group.ramen.location
  sku_name                  = var.managed_redis_sku
  high_availability_enabled = var.managed_redis_ha
  public_network_access     = "Enabled"
  default_database {
    access_keys_authentication_enabled = true
    client_protocol                    = "Encrypted"
    clustering_policy                  = "EnterpriseCluster" # one endpoint, no cluster-aware client needed
    eviction_policy                    = "NoEviction"        # throttle counters and tasks are never silently dropped
  }
}
