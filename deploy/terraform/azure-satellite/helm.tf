# Cluster add-ons installed from Terraform, as in the home module: Envoy Gateway for the edge, cert-manager for the
# Let's Encrypt certificate (install_addons=false to do it by hand).
provider "helm" {
  # Entra-integrated cluster: the user kubeconfig has no client certificate (kubelogin exec), so Terraform uses the
  # local admin credentials the cluster keeps for this purpose

  kubernetes {
    host                   = azurerm_kubernetes_cluster.satellite.kube_admin_config[0].host
    client_certificate     = base64decode(azurerm_kubernetes_cluster.satellite.kube_admin_config[0].client_certificate)
    client_key             = base64decode(azurerm_kubernetes_cluster.satellite.kube_admin_config[0].client_key)
    cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.satellite.kube_admin_config[0].cluster_ca_certificate)
  }
}

resource "helm_release" "envoy_gateway" {
  count            = var.install_addons ? 1 : 0
  name             = "eg"
  repository       = "oci://docker.io/envoyproxy"
  chart            = "gateway-helm"
  version          = var.envoy_gateway_chart_version
  namespace        = "envoy-gateway-system"
  create_namespace = true
  wait             = true
  timeout          = 900
  depends_on       = [azurerm_kubernetes_cluster.satellite]
}

resource "helm_release" "cert_manager" {
  count            = var.install_addons ? 1 : 0
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = var.cert_manager_chart_version
  namespace        = "cert-manager"
  create_namespace = true
  wait             = true
  timeout          = 900
  values = [yamlencode({
    crds = { enabled = true }
    config = {
      apiVersion       = "controller.config.cert-manager.io/v1alpha1"
      kind             = "ControllerConfiguration"
      enableGatewayAPI = true
    }
  })]
  depends_on = [helm_release.envoy_gateway]
}
