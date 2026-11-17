# Ramen satellite cluster on Azure (CONTRACTS §28.3, v1.0.0b): a worker-only AKS cluster in another location of the
# SAME subscription as the home apply (deploy/terraform/azure), with its own static public IP for the Envoy Gateway
# edge. The console in the home cluster drives it directly, authenticating to the API server with its workload
# identity's Entra token: the cluster is Entra-integrated (Kubernetes RBAC, no local Azure RBAC), so the identity
# appears as `User: <object id>`, which the satellite chart binds to the console ClusterRoles. Nothing else is created
# here: ACR, the storage account, Key Vault and the console identity are shared from the home apply.
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
  rg_name         = var.resource_group != "" ? data.azurerm_resource_group.existing[0].name : azurerm_resource_group.satellite[0].name
  rg_id           = var.resource_group != "" ? data.azurerm_resource_group.existing[0].id : azurerm_resource_group.satellite[0].id
  public_hostname = "${replace(azurerm_public_ip.edge.ip_address, ".", "-")}.sslip.io"
}

# Own resource group by default (ramen-<suffix>-<location>); `resource_group` reuses an existing one (the home one, say).
resource "azurerm_resource_group" "satellite" {
  count    = var.resource_group == "" ? 1 : 0
  name     = "${var.name}-${local.suffix}-${var.location}"
  location = var.location
}

data "azurerm_resource_group" "existing" {
  count = var.resource_group != "" ? 1 : 0
  name  = var.resource_group
}

# §28.1: the console in the home cluster calls satellite workers by pod IP. The home cluster is CNI overlay (its pod
# IPs are cluster-local), so the home side only needs routes; the satellite runs Azure CNI WITHOUT overlay in its
# own VNet, so its pods carry VNet addresses that the peering below routes, and the NSG lets the home VNet in on 8080.
resource "azurerm_virtual_network" "satellite" {
  name                = "${var.name}-${local.suffix}-${var.location}"
  location            = var.location
  resource_group_name = local.rg_name
  address_space       = [var.vnet_cidr]
}

resource "azurerm_subnet" "nodes" {
  name                 = "nodes"
  resource_group_name  = local.rg_name
  virtual_network_name = azurerm_virtual_network.satellite.name
  address_prefixes     = [var.vnet_cidr]
}

data "azurerm_virtual_network" "home" {
  name                = var.home_vnet_name
  resource_group_name = var.home_vnet_resource_group
}

resource "azurerm_network_security_group" "nodes" {
  name                = "${var.name}-${local.suffix}-${var.location}-nodes"
  location            = var.location
  resource_group_name = local.rg_name
  security_rule {
    name                       = "ramen-home-to-workers"
    priority                   = 200
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8080"
    source_address_prefixes    = data.azurerm_virtual_network.home.address_space
    destination_address_prefix = "*"
  }
  # the edge: Envoy's LoadBalancer on 80 (ACME HTTP-01, redirect) and 443 — a subnet NSG blocks it otherwise
  security_rule {
    name                       = "ramen-edge"
    priority                   = 210
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["80", "443"]
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "nodes" {
  subnet_id                 = azurerm_subnet.nodes.id
  network_security_group_id = azurerm_network_security_group.nodes.id
}

resource "azurerm_virtual_network_peering" "to_home" {
  name                      = "to-home"
  resource_group_name       = local.rg_name
  virtual_network_name      = azurerm_virtual_network.satellite.name
  remote_virtual_network_id = var.home_vnet_id
  allow_forwarded_traffic   = true
}

resource "azurerm_virtual_network_peering" "from_home" {
  name                      = "to-${var.name}-${local.suffix}-${var.location}"
  resource_group_name       = var.home_vnet_resource_group
  virtual_network_name      = var.home_vnet_name
  remote_virtual_network_id = azurerm_virtual_network.satellite.id
  allow_forwarded_traffic   = true
}

# A bring-your-own subnet needs the cluster identity to manage it before the cluster exists: user-assigned.
resource "azurerm_user_assigned_identity" "cluster" {
  name                = "${var.name}-${local.suffix}-${var.location}-cluster"
  resource_group_name = local.rg_name
  location            = var.location
}

resource "azurerm_role_assignment" "cluster_network" {
  scope                = azurerm_virtual_network.satellite.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_user_assigned_identity.cluster.principal_id
}

resource "azurerm_kubernetes_cluster" "satellite" {
  name                      = var.name
  location                  = var.location
  resource_group_name       = local.rg_name
  dns_prefix                = "${var.name}-${local.suffix}-${var.location}"
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
    vnet_subnet_id       = azurerm_subnet.nodes.id
    upgrade_settings { max_surge = "10%" }
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.cluster.id]
  }
  # S6b: Calico enforces the zones' NetworkPolicies, the same engine as the home cluster; on this node-subnet CNI
  # azurerm refuses Cilium (it needs overlay or a pod subnet). On an existing satellite it may force a replacement.
  network_profile {
    network_plugin    = "azure" # no overlay: pod IPs come from the nodes subnet and route over the peering
    network_policy    = "calico"
    load_balancer_sku = "standard"
  }
  depends_on = [azurerm_role_assignment.cluster_network, azurerm_subnet_network_security_group_association.nodes]
  # Entra ID for the API server (§28.2): the console's Entra token names it by object id; Kubernetes RBAC decides
  # (azure_rbac_enabled = false). Local accounts stay on for Terraform's own Helm provider below.
  azure_active_directory_role_based_access_control {
    tenant_id          = data.azurerm_client_config.me.tenant_id
    azure_rbac_enabled = false
  }
  lifecycle { ignore_changes = [default_node_pool[0].node_count] } # the autoscaler owns the count

}

# The home ACR is shared: this cluster's kubelet pulls the worker images from it. §38.2: the home registry runs with
# repository permissions (ABAC), where ACR honours the "Container Registry Repository *" roles, not AcrPull.
resource "azurerm_role_assignment" "kubelet_acr_pull" {
  count                = var.acr_id != "" ? 1 : 0
  scope                = var.acr_id
  role_definition_name = "Container Registry Repository Reader"
  principal_id         = azurerm_kubernetes_cluster.satellite.kubelet_identity[0].object_id
}

# The edge's address, in the node resource group as in the home module (bound by the AKS cloud provider, no extra role).
resource "azurerm_public_ip" "edge" {
  name                = "ramen-edge"
  resource_group_name = azurerm_kubernetes_cluster.satellite.node_resource_group
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = length(var.node_zones) > 0 ? ["1", "2", "3"] : null
  domain_name_label   = "${var.name}-${local.suffix}-${var.location}"
}

# The console's workload identity may read this cluster (endpoint, OIDC issuer) and fetch user credentials; what it
# may then do inside is the chart's ClusterRoleBinding (Kubernetes RBAC), not an Azure role.
resource "azurerm_role_assignment" "console_cluster" {
  for_each             = toset(["Azure Kubernetes Service Cluster User Role", "Reader"])
  scope                = azurerm_kubernetes_cluster.satellite.id
  role_definition_name = each.value
  principal_id         = var.console_principal_id
}
