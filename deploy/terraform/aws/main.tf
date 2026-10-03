# Ramen on AWS (CONTRACTS §8, v0.3.0). Verified live on a real account, v0.5.6 N10 (2026-10-03).
# Mirror of ../gcp: EKS (one small managed node group), DynamoDB single-table state, one S3 groups bucket, ECR repos,
# console IAM role (IRSA), AWS Load Balancer Controller + Fluent Bit → CloudWatch Logs via Helm, self-signed cert in ACM.
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

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.ramen.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.ramen.id
  }
  tags = local.tags
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
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
  depends_on = [aws_iam_role_policy_attachment.cluster]
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

resource "aws_eks_node_group" "ramen" {
  cluster_name    = aws_eks_cluster.ramen.name
  node_group_name = var.name
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
  tags       = local.tags
  depends_on = [aws_iam_role_policy_attachment.node]
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

resource "aws_ecr_repository" "images" {
  for_each     = toset(["console", "worker"])
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
