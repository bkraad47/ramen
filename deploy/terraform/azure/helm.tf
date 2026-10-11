# Cluster add-ons installed from Terraform (install_addons=false to do it by hand; see docs/wiki/deploy-azure.md):
# Envoy Gateway (Gateway API controller + CRDs) for the edge, cert-manager for the Let's Encrypt certificate.
provider "helm" {
  kubernetes {
    host                   = azurerm_kubernetes_cluster.ramen.kube_config[0].host
    client_certificate     = base64decode(azurerm_kubernetes_cluster.ramen.kube_config[0].client_certificate)
    client_key             = base64decode(azurerm_kubernetes_cluster.ramen.kube_config[0].client_key)
    cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.ramen.kube_config[0].cluster_ca_certificate)
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
  depends_on       = [azurerm_kubernetes_cluster.ramen]
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
    # HTTP-01 through a Gateway (cert-manager writes the solver HTTPRoute on the console Gateway's port-80 listener)
    config = {
      apiVersion       = "controller.config.cert-manager.io/v1alpha1"
      kind             = "ControllerConfiguration"
      enableGatewayAPI = true
    }
  })]
  depends_on = [helm_release.envoy_gateway] # Gateway API CRDs first, or cert-manager disables its Gateway support
}
