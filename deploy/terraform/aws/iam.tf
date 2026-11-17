# IAM roles for service accounts (IRSA). The console role is scoped so it can only manage `/ramen/` roles (and only
# with the worker permissions boundary attached), `ramen/*` secrets, the groups bucket, the state table and the
# `ramen` web ACL / `ramen-*` IP sets (SEC-10, CONTRACTS §11).
locals {
  console_ksa = "system:serviceaccount:ramen-system:console"
  waf_web_acl = "arn:aws:wafv2:${var.region}:${local.account}:regional/webacl/${var.alb_group}/*"
  waf_ip_sets = "arn:aws:wafv2:${var.region}:${local.account}:regional/ipset/ramen-*/*"
  alb_arns    = "arn:aws:elasticloadbalancing:${var.region}:${local.account}:loadbalancer/app/k8s-${var.alb_group}*/*"
}

# Permissions boundary for every worker role the console creates: whatever the SA policy engine (policy/permissions.py)
# grants, a worker can never exceed this. The console must pass it on iam:CreateRole (see the console policy below).
# §33.4a (D79): the bucket and secret statements are keyed on the role's `group` tag (the console tags every
# `ramen-<group>-<zone>` role it creates — cloud/aws_api.py ensure_role — and role tags become aws:PrincipalTag in the
# IRSA session), so even a mis-written inline policy cannot reach another group's prefix `<bucket>/<other>/` or its
# `ramen/<other>/*` secrets. The inline policies the console writes carry the same s3:prefix / object-ARN limits.
data "aws_iam_policy_document" "worker_boundary" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.groups.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["$${aws:PrincipalTag/group}/*", "$${aws:PrincipalTag/group}/"]
    }
  }
  statement { # carries no s3:prefix key, so it cannot sit under the condition above
    actions   = ["s3:GetBucketLocation"]
    resources = [aws_s3_bucket.groups.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.groups.arn}/$${aws:PrincipalTag/group}/*"]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${var.region}:${local.account}:secret:ramen/$${aws:PrincipalTag/group}/*"]
  }
  statement {
    actions = ["logs:CreateLogStream", "logs:PutLogEvents", "cloudwatch:PutMetricData", "sns:Publish",
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
    "kms:Decrypt", "bedrock:InvokeModel"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "worker_boundary" {
  name   = "ramen-worker-boundary"
  path   = "/ramen/"
  policy = data.aws_iam_policy_document.worker_boundary.json
  tags   = local.tags
}

data "aws_iam_policy_document" "irsa_trust" {
  for_each = {
    console    = local.console_ksa
    alb        = "system:serviceaccount:kube-system:aws-load-balancer-controller"
    fluent_bit = "system:serviceaccount:kube-system:aws-for-fluent-bit"
    evals      = "system:serviceaccount:ramen-system:ramen-evals"
    ebs_csi    = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
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
    resources = [ # ramen-console/*: the console's own secrets (1.0.0), outside every worker's boundary
      "arn:aws:secretsmanager:${var.region}:${local.account}:secret:ramen/*",
      "arn:aws:secretsmanager:${var.region}:${local.account}:secret:ramen-console/*",
    ]
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
  # §33.4a: no iam:PassRole anywhere — IRSA binds a role to a KSA by annotation, the console never passes a role to a
  # service. The role actions below are the only IAM writes and all stop at the /ramen/ path.
  statement { # create_service_account / detach_group: roles under /ramen/ only ...
    actions = ["iam:DeleteRole", "iam:GetRole", "iam:UpdateAssumeRolePolicy", "iam:TagRole",
    "iam:DeleteRolePolicy", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:DetachRolePolicy"]
    resources = ["arn:aws:iam::${local.account}:role/ramen/*"]
  }
  statement { # ... and only with the worker (zone roles) or build (§38.2 group build roles) permissions boundary, so a
    # compromised console cannot self-escalate
    actions   = ["iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy"]
    resources = ["arn:aws:iam::${local.account}:role/ramen/*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.worker_boundary.arn, aws_iam_policy.build_boundary.arn]
    }
  }
  statement { # §38.2: the per-group image repositories ramen/groups/* — created, tagged, deleted; never pushed to
    actions = ["ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:TagResource", "ecr:DeleteRepository",
    "ecr:BatchDeleteImage", "ecr:ListImages"]
    resources = ["arn:aws:ecr:${var.region}:${local.account}:repository/ramen/groups/*"]
  }
  statement {
    actions   = ["iam:ListRoles"]
    resources = ["*"]
  }
  statement { # set_ip_rules: the `ramen` web ACL and `ramen-*` IP sets only
    actions = ["wafv2:CreateIPSet", "wafv2:GetIPSet", "wafv2:UpdateIPSet", "wafv2:DeleteIPSet",
    "wafv2:CreateWebACL", "wafv2:GetWebACL", "wafv2:UpdateWebACL", "wafv2:DeleteWebACL", "wafv2:TagResource"]
    resources = [local.waf_web_acl, local.waf_ip_sets]
  }
  statement {
    actions   = ["wafv2:ListWebACLs", "wafv2:ListIPSets"]
    resources = ["*"]
  }
  statement {
    actions   = ["wafv2:AssociateWebACL", "wafv2:DisassociateWebACL"]
    resources = [local.waf_web_acl, local.alb_arns]
  }
  statement { # names no web ACL, so IAM checks the bare regional/webacl ARN (read-only)
    actions   = ["wafv2:GetWebACLForResource"]
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

# §38.1: the judge proxy calls Bedrock for a group's LLM rails; workers never call a provider for rails (the worker
# boundary keeps bedrock:InvokeModel only for an approved `ai.user` permission request on tool code).
data "aws_iam_policy_document" "console_bedrock" {
  statement {
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["arn:aws:bedrock:*::foundation-model/*",
    "arn:aws:bedrock:*:${local.account}:inference-profile/*"]
  }
  statement { # Bedrock API keys (bearer tokens) minted from the role's credentials; not resource-scoped
    actions   = ["bedrock:CallWithBearerToken"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "console_bedrock" {
  count  = var.console_bedrock ? 1 : 0
  name   = "ramen-console-bedrock"
  role   = aws_iam_role.console.id
  policy = data.aws_iam_policy_document.console_bedrock.json
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

# EBS CSI driver (managed addon, main.tf): the controller creates, attaches and deletes the PVCs' EBS volumes
resource "aws_iam_role" "ebs_csi" {
  name               = "ramen-ebs-csi"
  path               = "/ramen/"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust["ebs_csi"].json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_cloudwatch_log_group" "application" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

# §38.2 (D85, F2): no shared builder role. The console creates, per group, the ECR repository
# ramen/groups/<group>/worker and the role /ramen/build/ramen-build-<group> (IRSA for ramen-system/ramen-build-<group>)
# whose inline policy pushes to that repository only. Every such role carries this boundary: push only under
# ramen/groups/<its ramen-build tag>/, pull from the ramen/* repositories (the worker base image is the build's FROM),
# nothing else — so no build identity can ever write the platform repositories ramen/{console,worker,skillspector}.
data "aws_iam_policy_document" "build_boundary" {
  statement { # registry login: not scopable to a repository
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = ["arn:aws:ecr:${var.region}:${local.account}:repository/ramen/*"]
  }
  statement {
    actions   = ["ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"]
    resources = ["arn:aws:ecr:${var.region}:${local.account}:repository/ramen/groups/$${aws:PrincipalTag/ramen-build}/*"]
  }
}

resource "aws_iam_policy" "build_boundary" {
  name   = "ramen-build-boundary"
  path   = "/ramen/"
  policy = data.aws_iam_policy_document.build_boundary.json
  tags   = local.tags
}

# §33.4a (D79): the identity the evals and SkillSpector scan Jobs run as (KSA ramen-system/ramen-evals). They execute
# a group repo's promptfoo config, so this role may only write its own log streams: no ECR, no bucket, no secrets.
resource "aws_iam_role" "evals" {
  name               = "ramen-evals"
  path               = "/ramen/"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust["evals"].json
  tags               = local.tags
}

data "aws_iam_policy_document" "evals" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.application.arn}:*"]
  }
}

resource "aws_iam_role_policy" "evals" {
  name   = "ramen-evals"
  role   = aws_iam_role.evals.id
  policy = data.aws_iam_policy_document.evals.json
}
