# Ramen satellite cluster on AWS (CONTRACTS §28.3, v1.0.0b): a worker-only EKS cluster in another region of the SAME
# account as the home apply (deploy/terraform/aws), with its own ALB (made by the Load Balancer Controller from the
# satellite chart's Ingress) and self-signed ACM certificate. The console in the home cluster drives it directly:
# an EKS access entry maps the console pod role to the Kubernetes group `ramen-console`, which the satellite chart
# binds to the console ClusterRoles. Nothing else is created here: DynamoDB, the groups bucket, ECR, the console role and
# the worker / build permissions boundaries are shared from the home apply (IAM is global; builds run at home).
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws  = { source = "hashicorp/aws", version = "~> 5.70" }
    tls  = { source = "hashicorp/tls", version = "~> 4.0" }
    helm = { source = "hashicorp/helm", version = "~> 2.16" }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "me" {}
data "aws_availability_zones" "available" { state = "available" }

locals {
  account      = data.aws_caller_identity.me.account_id
  name         = coalesce(var.name, "ramen-${var.region}")
  azs          = slice(data.aws_availability_zones.available.names, 0, 2)
  log_group    = "/aws/containerinsights/${local.name}/application"
  oidc         = replace(aws_eks_cluster.satellite.identity[0].oidc[0].issuer, "https://", "")
  console_role = coalesce(var.console_role_arn, "arn:aws:iam::${local.account}:role/ramen/ramen-console")
  tags         = { app = "ramen", ramen-satellite = var.region }
}

# --- network: two public subnets, no NAT (same layout as home) ---
resource "aws_vpc" "satellite" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(local.tags, { Name = local.name })
}

resource "aws_internet_gateway" "satellite" {
  vpc_id = aws_vpc.satellite.id
  tags   = local.tags
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.satellite.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags = merge(local.tags, {
    Name                                  = "${local.name}-public-${count.index}"
    "kubernetes.io/role/elb"              = "1"
    "kubernetes.io/cluster/${local.name}" = "shared"
  })
}

# D89: pod subnets via VPC CNI custom networking, as in the home module (no public IPs, no NAT, no elb role tag).
resource "aws_subnet" "pods" {
  count                   = 2
  vpc_id                  = aws_vpc.satellite.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, 4 + count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags                    = merge(local.tags, { Name = "${local.name}-pods-${count.index}", "ramen.io/subnet" = "pods" })
}

# destroy-only sweeps, as in the home module (0.7.5): leftover node ENIs block the subnets, the cluster SG blocks the VPC.
resource "terraform_data" "eni_sweep" {
  input      = { vpc = aws_vpc.satellite.id, region = var.region }
  depends_on = [aws_subnet.public, aws_subnet.pods]
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "bash ${path.module}/../aws/scripts/eni_sweep.sh ${self.input.vpc} ${self.input.region}"
  }
}

resource "terraform_data" "sg_sweep" {
  input      = { vpc = aws_vpc.satellite.id, region = var.region, name = local.name }
  depends_on = [aws_vpc.satellite]
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "bash ${path.module}/../aws/scripts/eni_sweep.sh ${self.input.vpc} ${self.input.region} cluster-sg ${self.input.name}"
  }
}

# No inline `route` blocks: a table that manages its routes inline wipes every standalone `aws_route` on each apply —
# the satellite modules add their peering routes to this table as standalone resources (§28.1).
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.satellite.id
  tags   = local.tags
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.satellite.id
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "pods" {
  count          = 2
  subnet_id      = aws_subnet.pods[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- EKS ---
data "aws_iam_policy_document" "eks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${local.name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_trust.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "satellite" {
  name     = local.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn
  vpc_config {
    subnet_ids              = aws_subnet.public[*].id
    endpoint_public_access  = true
    endpoint_private_access = true
  }
  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }
  tags       = local.tags
  depends_on = [aws_iam_role_policy_attachment.cluster, terraform_data.sg_sweep]
}

# S6b: EKS enforces NetworkPolicies (the chart's ramen-system policies, the console's per-zone worker policies) only
# with the VPC CNI's network-policy agent on. The managed addon adopts the self-managed aws-node DaemonSet
# (OVERWRITE) and turns the agent on; the node group waits for it so new nodes start enforcing. On an existing
# cluster this rolls aws-node in place — but add this cluster's alb_cidrs to the home console's
# RAMEN_AWS_ALB_CIDRS first, or the now-enforced policies cut this ALB off (deploy-aws.md, satellites).
# D89: custom networking with one ENIConfig per AZ (pod subnet + cluster security group), as in the home module.
resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.satellite.name
  addon_name                  = "vpc-cni"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  configuration_values = jsonencode({
    enableNetworkPolicy = "true"
    env = {
      AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG = "true"
      ENI_CONFIG_LABEL_DEF               = "topology.kubernetes.io/zone"
    }
    eniConfig = {
      create  = true
      region  = var.region
      subnets = { for s in aws_subnet.pods : s.availability_zone => { id = s.id, securityGroups = [aws_eks_cluster.satellite.vpc_config[0].cluster_security_group_id] } }
    }
  })
  tags = local.tags
}

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${local.name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly", # pulls from the home region's ECR (cross-region pull)
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# D89: renamed so a pre-D89 group is replaced once (new nodes first); EKS sets max pods (t3.small: 8), as at home.
resource "aws_eks_node_group" "satellite" {
  cluster_name    = aws_eks_cluster.satellite.name
  node_group_name = "${local.name}-podnet"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.public[*].id
  instance_types  = [var.node_instance_type]
  capacity_type   = "ON_DEMAND"
  scaling_config {
    desired_size = var.node_count
    min_size     = 1
    max_size     = var.node_max
  }
  update_config { max_unavailable = 1 }
  lifecycle { create_before_destroy = true }
  tags       = local.tags
  depends_on = [aws_iam_role_policy_attachment.node, terraform_data.eni_sweep, aws_eks_addon.vpc_cni]
}

# --- the console's access to this cluster (§28.2): its pod role as Kubernetes group `ramen-console` ---
resource "aws_eks_access_entry" "console" {
  cluster_name      = aws_eks_cluster.satellite.name
  principal_arn     = local.console_role
  kubernetes_groups = [var.console_group]
  type              = "STANDARD"
  tags              = local.tags
}

# The home console policy scopes EKS, WAF and ALB actions to the home cluster and region; this inline policy on the
# same role adds the satellite's. Separate policy name, so the home state never sees it.
data "aws_iam_policy_document" "console_satellite" {
  statement {
    actions   = ["eks:DescribeCluster"]
    resources = [aws_eks_cluster.satellite.arn]
  }
  statement {
    actions = ["wafv2:CreateIPSet", "wafv2:GetIPSet", "wafv2:UpdateIPSet", "wafv2:DeleteIPSet",
    "wafv2:CreateWebACL", "wafv2:GetWebACL", "wafv2:UpdateWebACL", "wafv2:DeleteWebACL", "wafv2:TagResource"]
    resources = [
      "arn:aws:wafv2:${var.region}:${local.account}:regional/webacl/${var.alb_group}/*",
      "arn:aws:wafv2:${var.region}:${local.account}:regional/ipset/ramen-*/*",
    ]
  }
  statement {
    actions = ["wafv2:AssociateWebACL", "wafv2:DisassociateWebACL"]
    resources = [
      "arn:aws:wafv2:${var.region}:${local.account}:regional/webacl/${var.alb_group}/*",
      "arn:aws:elasticloadbalancing:${var.region}:${local.account}:loadbalancer/app/k8s-${var.alb_group}*/*",
    ]
  }
}

resource "aws_iam_role_policy" "console_satellite" {
  name   = "ramen-satellite-${var.region}"
  role   = element(split("/", local.console_role), length(split("/", local.console_role)) - 1)
  policy = data.aws_iam_policy_document.console_satellite.json
}

# --- IRSA for this cluster: the LB controller, Fluent Bit and the per-zone worker roles the console creates trust it ---
data "tls_certificate" "oidc" {
  url = aws_eks_cluster.satellite.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.satellite.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc.certificates[0].sha1_fingerprint]
  tags            = local.tags
}

data "aws_iam_policy_document" "irsa_trust" {
  for_each = {
    alb        = "system:serviceaccount:kube-system:aws-load-balancer-controller"
    fluent_bit = "system:serviceaccount:kube-system:aws-for-fluent-bit"
  }
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc}:sub"
      values   = [each.value]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "alb_controller" {
  name               = "${local.name}-alb-controller"
  path               = "/ramen/"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust["alb"].json
  tags               = local.tags
}

resource "aws_iam_role_policy" "alb_controller" {
  name   = "AWSLoadBalancerControllerIAMPolicy"
  role   = aws_iam_role.alb_controller.id
  policy = file("${path.module}/../aws/alb-controller-iam-policy.json")
}

resource "aws_iam_role" "fluent_bit" {
  name               = "${local.name}-fluent-bit"
  path               = "/ramen/"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust["fluent_bit"].json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "fluent_bit" {
  role       = aws_iam_role.fluent_bit.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_cloudwatch_log_group" "application" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

# --- edge TLS: self-signed certificate imported into ACM, wildcard SAN for this region's ALB domain (as home) ---
resource "tls_private_key" "edge" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "edge" {
  private_key_pem       = tls_private_key.edge.private_key_pem
  validity_period_hours = 8760
  allowed_uses          = ["key_encipherment", "digital_signature", "server_auth"]
  dns_names             = ["${local.name}.local", "*.${var.region}.elb.amazonaws.com"]
  subject {
    common_name  = local.name
    organization = "ramen"
  }
}

resource "aws_acm_certificate" "edge" {
  private_key      = tls_private_key.edge.private_key_pem
  certificate_body = tls_self_signed_cert.edge.cert_pem
  tags             = local.tags
}
