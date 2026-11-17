output "location" { value = var.location }
output "subscription_id" { value = var.subscription_id }
output "resource_group" { value = local.rg_name }
output "cluster" { value = azurerm_kubernetes_cluster.satellite.name }
output "node_resource_group" { value = azurerm_kubernetes_cluster.satellite.node_resource_group }
output "oidc_issuer" { value = azurerm_kubernetes_cluster.satellite.oidc_issuer_url }
output "vnet_id" { value = azurerm_virtual_network.satellite.id }
output "vnet_cidr" { value = var.vnet_cidr }
output "public_ip" { value = azurerm_public_ip.edge.ip_address }
output "public_hostname" { value = local.public_hostname } # the satellite's `host` on Config → Clusters (Add satellite)
output "kubeconfig_command" {
  value = "az aks get-credentials --resource-group ${local.rg_name} --name ${azurerm_kubernetes_cluster.satellite.name} --overwrite-existing --admin"
}
# Everything `helm install ramen-satellite` needs, ready to paste (deploy-azure.md, satellite clusters)
output "helm_set" {
  value = join(" ", [
    "--set provider=azure,region=${var.location}",
    "--set console.identity=${var.console_principal_id}",
    "--set azure.nodeResourceGroup=${azurerm_kubernetes_cluster.satellite.node_resource_group},azure.publicIpName=${azurerm_public_ip.edge.name}",
    "--set azure.publicHostname=${local.public_hostname}",
  ])
}
