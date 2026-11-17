output "resource_group" { value = azurerm_resource_group.ramen.name }
output "location" { value = var.location }
output "subscription_id" { value = var.subscription_id }
output "cluster_name" { value = azurerm_kubernetes_cluster.ramen.name }
output "node_resource_group" { value = azurerm_kubernetes_cluster.ramen.node_resource_group }
output "acr" { value = azurerm_container_registry.ramen.login_server }
output "storage_account" { value = azurerm_storage_account.ramen.name }
output "key_vault" { value = azurerm_key_vault.ramen.name }
output "oidc_issuer" { value = azurerm_kubernetes_cluster.ramen.oidc_issuer_url }
output "console_client_id" { value = azurerm_user_assigned_identity.console.client_id }
output "acr_id" { value = azurerm_container_registry.ramen.id }                     # §38.2: scope of the per-group build role assignments
output "evals_client_id" { value = azurerm_user_assigned_identity.evals.client_id } # helm: evals.clientId (§33.4a)
# §28.1: the AKS-managed VNet (in the node resource group; 10.224.0.0/12 by default) a satellite module peers with
# (deploy/terraform/azure-satellite vars home_vnet_id/home_vnet_name/home_vnet_resource_group)
data "azurerm_resources" "vnet" {
  resource_group_name = azurerm_kubernetes_cluster.ramen.node_resource_group
  type                = "Microsoft.Network/virtualNetworks"
}
output "vnet_id" { value = one(data.azurerm_resources.vnet.resources[*].id) }
output "vnet_name" { value = one(data.azurerm_resources.vnet.resources[*].name) }
output "vnet_resource_group" { value = azurerm_kubernetes_cluster.ramen.node_resource_group }
output "public_ip" { value = azurerm_public_ip.edge.ip_address }
output "public_ip_name" { value = azurerm_public_ip.edge.name }
output "public_hostname" { value = local.public_hostname }
output "kubeconfig_command" {
  value = "az aks get-credentials --resource-group ${azurerm_resource_group.ramen.name} --name ${azurerm_kubernetes_cluster.ramen.name} --overwrite-existing"
}
# Everything `helm install ramen` needs, ready to paste (deploy-azure.md step 3)
output "helm_set" {
  value = join(" ", [
    "--set provider=azure,region=${var.location}",
    "--set azure.subscriptionId=${var.subscription_id},azure.resourceGroup=${azurerm_resource_group.ramen.name}",
    "--set azure.nodeResourceGroup=${azurerm_kubernetes_cluster.ramen.node_resource_group},azure.publicIpName=${azurerm_public_ip.edge.name}",
    "--set azure.acr=${azurerm_container_registry.ramen.login_server},azure.storageAccount=${azurerm_storage_account.ramen.name}",
    "--set azure.keyVault=${azurerm_key_vault.ramen.name},azure.clientId=${azurerm_user_assigned_identity.console.client_id}",
    "--set azure.oidcIssuer=${azurerm_kubernetes_cluster.ramen.oidc_issuer_url},azure.publicHostname=${local.public_hostname}",
    "--set-string azure.tenantId=${data.azurerm_client_config.me.tenant_id}",
    "--set azure.zonal=${length(var.node_zones) > 0}",
    "--set builder.enabled=true", # §38.2: per-group build identities, no shared builder
    "--set evals.clientId=${azurerm_user_assigned_identity.evals.client_id}",
    local.engines_set,
    local.redis_set,
  ])
  sensitive = true # carries the Managed Redis key when managed_redis is on; `terraform output -raw` still prints it
}
# 1.0.0c: the Managed Redis URL the chart writes into Secret ramen-redis (redis.externalUrl); also part of helm_set
output "redis_url" {
  value     = local.redis_url
  sensitive = true
}
locals {
  redis_url = var.managed_redis ? format(
    "rediss://:%s@%s:%d/0",
    azurerm_managed_redis.ramen[0].default_database[0].primary_access_key,
    azurerm_managed_redis.ramen[0].hostname,
    azurerm_managed_redis.ramen[0].default_database[0].port,
  ) : ""
  redis_set = var.managed_redis ? "--set-string redis.externalUrl=${local.redis_url}" : ""
}
# §34.1 optional engines; also part of helm_set above
output "engines_set" { value = local.engines_set }
locals {
  engines_set = "--set otel.collector=${var.otel_collector},evals.engine=${var.evals_engine},skillspector.engine=${var.skillspector_engine}"
}
