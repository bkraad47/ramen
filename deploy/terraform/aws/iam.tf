# IAM roles for service accounts (IRSA). The console role is scoped so it can only manage `/ramen/` roles,
# `ramen/*` secrets, the groups bucket, the state table and the regional WAF resources.
locals {
  console_ksa = "system:serviceaccount:ramen-system:console"
}

data "aws_iam_policy_document" "irsa_trust" {
  for_each = {
    console    = local.console_ksa
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

resource "aws_iam_role" "console" {
  name               = "ramen-console"
  path               = "/ramen/"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust["console"].json
  tags               = local.tags
}

data "aws_iam_policy_document" "console" {
  statement { # groups bucket: repo sync writes <group>/ prefixes
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.groups.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.groups.arn}/*"]
  }
  statement { # secrets backend `aws`: ramen/<group>/<env>/<zone>/<NAME>
    actions = ["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue", "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
    "secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret", "secretsmanager:TagResource", "secretsmanager:UpdateSecret"]
    resources = ["arn:aws:secretsmanager:${var.region}:${local.account}:secret:ramen/*"]
  }
  statement {
    actions   = ["secretsmanager:ListSecrets"]
    resources = ["*"]
  }
  statement { # state table (RAMEN_STORE=dynamodb; ensure_table also lists/creates)
    actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan",
    "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem", "dynamodb:DescribeTable", "dynamodb:CreateTable"]
    resources = [aws_dynamodb_table.state.arn, "${aws_dynamodb_table.state.arn}/index/*"]
  }
  statement {
    actions   = ["dynamodb:ListTables"]
    resources = ["*"]
  }
  statement { # OIDC issuer lookup for per-group worker roles
    actions   = ["eks:DescribeCluster"]
    resources = [aws_eks_cluster.ramen.arn]
  }
  statement { # create_service_account / detach_group: roles under /ramen/ only
    actions = ["iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateAssumeRolePolicy", "iam:TagRole", "iam:PutRolePolicy",
    "iam:DeleteRolePolicy", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:DetachRolePolicy"]
    resources = ["arn:aws:iam::${local.account}:role/ramen/*"]
  }
  statement {
    actions   = ["iam:ListRoles"]
    resources = ["*"]
  }
  statement { # set_ip_rules: regional IP sets + web ACL, associated to the ALB
    actions   = ["wafv2:*"]
    resources = ["arn:aws:wafv2:${var.region}:${local.account}:regional/*"]
  }
  statement {
    actions   = ["wafv2:ListWebACLs", "wafv2:ListIPSets", "wafv2:GetWebACLForResource", "wafv2:AssociateWebACL", "wafv2:DisassociateWebACL"]
    resources = ["*"]
  }
  statement { # rebalance / ip-rules discover the IngressGroup ALB by tag
    actions   = ["elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags", "elasticloadbalancing:SetWebAcl"]
    resources = ["*"]
  }
  statement { # logs(): Insights on the Container Insights application log group
    actions   = ["logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery", "logs:DescribeLogGroups"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "console" {
  name   = "ramen-console"
  role   = aws_iam_role.console.id
  policy = data.aws_iam_policy_document.console.json
}

# AWS Load Balancer Controller (official policy, vendored from kubernetes-sigs/aws-load-balancer-controller v2.13.0)
resource "aws_iam_role" "alb_controller" {
  name               = "ramen-alb-controller"
  path               = "/ramen/"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust["alb"].json
  tags               = local.tags
}

resource "aws_iam_role_policy" "alb_controller" {
  name   = "AWSLoadBalancerControllerIAMPolicy"
  role   = aws_iam_role.alb_controller.id
  policy = file("${path.module}/alb-controller-iam-policy.json")
}

# Fluent Bit → CloudWatch Logs (Container Insights application log group)
resource "aws_iam_role" "fluent_bit" {
  name               = "ramen-fluent-bit"
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
