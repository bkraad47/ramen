# Cluster add-ons installed from Terraform (set install_addons=false to do it by hand; see deploy/README.md).
provider "helm" {
  kubernetes {
    host                   = aws_eks_cluster.ramen.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.ramen.certificate_authority[0].data)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", aws_eks_cluster.ramen.name, "--region", var.region]
    }
  }
}

resource "helm_release" "alb_controller" {
  count      = var.install_addons ? 1 : 0
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = var.alb_controller_chart_version
  namespace  = "kube-system"
  set {
    name  = "clusterName"
    value = aws_eks_cluster.ramen.name
  }
  set {
    name  = "region"
    value = var.region
  }
  set {
    name  = "vpcId"
    value = aws_vpc.ramen.id
  }
  set {
    name  = "serviceAccount.name"
    value = "aws-load-balancer-controller"
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.alb_controller.arn
  }
  depends_on = [aws_eks_node_group.ramen, aws_iam_openid_connect_provider.eks]
}

resource "helm_release" "fluent_bit" {
  count      = var.install_addons ? 1 : 0
  name       = "aws-for-fluent-bit"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-for-fluent-bit"
  version    = var.fluent_bit_chart_version
  namespace  = "kube-system"
  set {
    name  = "serviceAccount.name"
    value = "aws-for-fluent-bit"
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.fluent_bit.arn
  }
  set {
    name  = "cloudWatchLogs.enabled"
    value = "true"
  }
  set {
    name  = "cloudWatchLogs.region"
    value = var.region
  }
  set {
    name  = "cloudWatchLogs.logGroupName"
    value = local.log_group # the console's logs() queries this group: filter kubernetes.namespace_name / pod_name
  }
  set {
    name  = "cloudWatchLogs.logGroupTemplate"
    value = ""
  }
  set {
    name  = "cloudWatchLogs.autoCreateGroup"
    value = "false"
  }
  set {
    name  = "cloudWatchLogs.logRetentionDays"
    value = tostring(var.log_retention_days)
  }
  set {
    name  = "firehose.enabled"
    value = "false"
  }
  set {
    name  = "kinesis.enabled"
    value = "false"
  }
  set {
    name  = "elasticsearch.enabled"
    value = "false"
  }
  depends_on = [aws_eks_node_group.ramen, aws_iam_openid_connect_provider.eks, aws_cloudwatch_log_group.application]
}
