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
  ])
}
