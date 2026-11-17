# Ramen on AWS (CONTRACTS §8, v0.3.0). Verified live on a real account, v0.5.6 N10 (2026-10-03).
# Mirror of ../gcp: EKS (one small managed node group), DynamoDB single-table state, one S3 groups bucket, ECR repos,
# console IAM role (IRSA), EBS CSI driver addon, AWS Load Balancer Controller + Fluent Bit → CloudWatch Logs via Helm, self-signed cert in ACM.
# Nothing per-group is created here (the console's aws adapter does that at runtime).
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
  account       = data.aws_caller_identity.me.account_id
  groups_bucket = coalesce(var.groups_bucket, "ramen-${local.account}-groups")
  azs           = slice(data.aws_availability_zones.available.names, 0, 2)
  log_group     = "/aws/containerinsights/${var.name}/application"
  oidc          = replace(aws_eks_cluster.ramen.identity[0].oidc[0].issuer, "https://", "")
  tags          = { app = "ramen" }
}

# --- network: two public subnets, no NAT (cheapest multi-AZ layout that satisfies EKS + an internet-facing ALB) ---
resource "aws_vpc" "ramen" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(local.tags, { Name = var.name })
}

resource "aws_internet_gateway" "ramen" {
  vpc_id = aws_vpc.ramen.id
  tags   = local.tags
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.ramen.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags = merge(local.tags, {
    Name                                = "${var.name}-public-${count.index}"
    "kubernetes.io/role/elb"            = "1" # ALB controller subnet discovery
    "kubernetes.io/cluster/${var.name}" = "shared"
  })
}

# D89: pods take addresses here (VPC CNI custom networking, ENIConfig per AZ below), nodes and the ALB stay in the
# public /24s, so the worker NetworkPolicy can admit the ALB's subnets without admitting every pod. /20 per AZ
# (10.42.64.0/20, 10.42.80.0/20 for the default VPC). No public IPs and no NAT: pod traffic leaving the VPC is
# SNATed to the node's primary address (AWS_VPC_K8S_CNI_EXTERNALSNAT=false). No kubernetes.io/role/elb tag, so the
# Load Balancer Controller never places the ALB here.
resource "aws_subnet" "pods" {
  count                   = 2
  vpc_id                  = aws_vpc.ramen.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, 4 + count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags                    = merge(local.tags, { Name = "${var.name}-pods-${count.index}", "ramen.io/subnet" = "pods" })
}

# destroy only: the VPC CNI can leave a terminated node's interface behind, which blocks deleting the subnets (0.7.5).
# The node group depends on this, so destroy runs node group → sweep → subnets. Needs the aws CLI (as helm.tf does).
resource "terraform_data" "eni_sweep" {
  input      = { vpc = aws_vpc.ramen.id, region = var.region }
  depends_on = [aws_subnet.public, aws_subnet.pods]
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "bash ${path.module}/scripts/eni_sweep.sh ${self.input.vpc} ${self.input.region}"
  }
}

# destroy only: EKS can leave its `eks-cluster-sg-<name>-<n>` group behind, which blocks deleting the VPC (0.7.5).
# The cluster depends on this, so destroy runs cluster → sweep → VPC.
resource "terraform_data" "sg_sweep" {
  input      = { vpc = aws_vpc.ramen.id, region = var.region, name = var.name }
  depends_on = [aws_vpc.ramen]
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "bash ${path.module}/scripts/eni_sweep.sh ${self.input.vpc} ${self.input.region} cluster-sg ${self.input.name}"
  }
}

# No inline `route` blocks: a table that manages its routes inline wipes every standalone `aws_route` on each apply —
# the satellite modules add their peering routes to this table as standalone resources (§28.1).
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.ramen.id
  tags   = local.tags
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.ramen.id
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# the same table: local + peering routes for return traffic to pods (egress to the internet leaves via the node)
resource "aws_route_table_association" "pods" {
  count          = 2
  subnet_id      = aws_subnet.pods[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- EKS ---
resource "aws_iam_role" "cluster" {
  name               = "${var.name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_trust.json
  tags               = local.tags
}

data "aws_iam_policy_document" "eks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "ramen" {
  name     = var.name
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
# cluster this rolls aws-node in place — but set the chart's networkPolicy.awsAlbCidrs first (output network_set),
# or the now-enforced policies cut the ALB off (deploy-aws.md, upgrading).
# D89: custom networking — ipamd takes pod addresses from the ENIConfig named after the node's zone label, which the
# addon creates (one per AZ: the pod subnet, the cluster security group the ALB controller opens to the ALB). Only
# nodes launched after this get pod-subnet ENIs, hence the node group's name below.
resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.ramen.name
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
      subnets = { for s in aws_subnet.pods : s.availability_zone => { id = s.id, securityGroups = [aws_eks_cluster.ramen.vpc_config[0].cluster_security_group_id] } }
    }
  })
  tags = local.tags
}

resource "aws_iam_role" "node" {
  name               = "${var.name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
  tags               = local.tags
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

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# D89: "-podnet" — the name change replaces a pre-D89 group once (new nodes first, then the old ones drain), since
# nodes started before custom networking keep their pod ENIs in the public subnets. No launch template, so EKS sets
# max pods for custom networking itself: (ENIs - 1) x (IPs per ENI - 1) + 2, a t3.small 8 (was 11).
resource "aws_eks_node_group" "ramen" {
  cluster_name    = aws_eks_cluster.ramen.name
  node_group_name = "${var.name}-podnet"
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

# 1.0.0c: the only class EKS ships is the in-tree `gp2` (kubernetes.io/aws-ebs), which provisions nothing without
# this driver: the chart's promptfoo and Phoenix PVCs sat Pending. The class itself (`gp3`, encrypted, default) comes
# from the chart (storage.createAwsGp3 in engines_set): this module has no kubernetes provider, and CloudFormation
# needs the same path. After the node group: the node DaemonSet needs nodes or the addon never turns ACTIVE. One
# controller replica (a t3.small takes 8 pods since D89); volumes tagged app=ramen so a leftover one is findable.
resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.ramen.name
  addon_name                  = "aws-ebs-csi-driver"
  service_account_role_arn    = aws_iam_role.ebs_csi.arn
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  configuration_values = jsonencode({
    controller = { replicaCount = 1, extraVolumeTags = local.tags }
  })
  tags       = local.tags
  depends_on = [aws_eks_node_group.ramen]
}

# --- IRSA: OIDC provider for the cluster (console, LB controller, Fluent Bit and per-group worker roles trust it) ---
data "tls_certificate" "oidc" {
  url = aws_eks_cluster.ramen.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.ramen.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc.certificates[0].sha1_fingerprint]
  tags            = local.tags
}

# --- state, groups bucket, registries ---
resource "aws_dynamodb_table" "state" {
  name         = var.table
  billing_mode = "PAY_PER_REQUEST" # matches ramen_console.storage.dynamodb (pk/sk single table, on-demand)
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  tags = local.tags
}

resource "aws_s3_bucket" "groups" {
  bucket        = local.groups_bucket
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "groups" {
  bucket                  = aws_s3_bucket.groups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# The platform repositories (§38.2): no build role can push here (the build boundary allows push only under
# ramen/groups/). Group images go to ramen/groups/<group>/worker, created by the console on the group's first build.
resource "aws_ecr_repository" "images" {
  for_each     = toset(["console", "worker", "skillspector"])
  name         = "ramen/${each.value}"
  force_delete = true
  image_scanning_configuration { scan_on_push = true }
  tags = local.tags
}

# --- console TLS: self-signed certificate imported into ACM (no domain yet; mirrors GCP D17) ---
resource "tls_private_key" "console" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "console" {
  private_key_pem       = tls_private_key.console.private_key_pem
  validity_period_hours = 8760
  allowed_uses          = ["key_encipherment", "digital_signature", "server_auth"]
  # "ramen-console.local" alone fails real TLS clients (grpc hostname verification, not just curl -k/grpcurl
  # -insecure, which skip the check entirely): the ALB's real hostname isn't known until it's created, well
  # after this cert — fixed with a wildcard SAN for the region's ELB domain (AWS ALB hostnames are always
  # exactly one label under <region>.elb.amazonaws.com, so this matches any ALB this terraform creates here
  # without needing the literal hostname). Found live testing ramen-mcp-bridge --ca against a real ALB.
  dns_names = ["ramen-console.local", "*.${var.region}.elb.amazonaws.com"]
  subject {
    common_name  = "ramen-console"
    organization = "ramen"
  }
}

resource "aws_acm_certificate" "console" {
  private_key      = tls_private_key.console.private_key_pem
  certificate_body = tls_self_signed_cert.console.cert_pem
  tags             = local.tags
}
