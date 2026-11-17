output "cluster_name" { value = aws_eks_cluster.ramen.name }
output "region" { value = var.region }
output "account" { value = local.account }
output "ecr_console" { value = aws_ecr_repository.images["console"].repository_url }
output "ecr_worker" { value = aws_ecr_repository.images["worker"].repository_url }
output "console_role_arn" { value = aws_iam_role.console.arn }
output "builder_set" { value = "--set builder.enabled=true" }               # §38.2: per-group build roles, no shared builder
output "build_boundary_arn" { value = aws_iam_policy.build_boundary.arn }   # RAMEN_AWS_BUILD_BOUNDARY (console default matches)
output "evals_role_arn" { value = aws_iam_role.evals.arn }                  # helm: --set evals.roleArn=<this> (§33.4a)
output "worker_boundary_arn" { value = aws_iam_policy.worker_boundary.arn } # RAMEN_AWS_PERMISSIONS_BOUNDARY (console default matches)
output "groups_bucket" { value = aws_s3_bucket.groups.bucket }
output "dynamodb_table" { value = aws_dynamodb_table.state.name }
output "certificate_arn" { value = aws_acm_certificate.console.arn }
output "oidc_provider_arn" { value = aws_iam_openid_connect_provider.eks.arn }
# §28.1: a satellite module peers its VPC with this one (deploy/terraform/aws-satellite vars home_vpc_id/home_vpc_cidr/home_route_table_ids)
output "vpc_id" { value = aws_vpc.ramen.id }
output "vpc_cidr" { value = aws_vpc.ramen.cidr_block }
# S6b/D89: the ALB reaches pods from its public subnets; the chart's ramen-system policies and the console's zone
# policies (RAMEN_AWS_ALB_CIDRS) admit those, never the pod subnets — paste into `helm install ramen` (deploy-aws.md step 3)
output "network_set" { value = "--set networkPolicy.awsAlbCidrs={${join(",", aws_subnet.public[*].cidr_block)}}" }
output "alb_cidrs" { value = join(",", aws_subnet.public[*].cidr_block) }
output "pod_subnet_cidrs" { value = aws_subnet.pods[*].cidr_block }
output "route_table_ids" { value = [aws_route_table.public.id] }
output "log_group" { value = aws_cloudwatch_log_group.application.name }
output "kubeconfig_command" { value = "aws eks update-kubeconfig --name ${aws_eks_cluster.ramen.name} --region ${var.region}" }
# The ALB is created by the Load Balancer Controller from the console Ingress (helm chart), so its hostname is only
# known after `helm install ramen`: this output tells you how to read it.
output "console_url" {
  value = "https://$(kubectl -n ramen-system get ingress console -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
}
# §34.1 optional engines, ready to paste into `helm install ramen` (docs/wiki/deploy-*.md). On EKS also the class
# their PVCs (promptfoo, Phoenix) bind to: the chart's gp3 (EBS CSI addon, main.tf); EKS has no usable default.
output "engines_set" { value = local.engines_set }
locals {
  engines_set = "--set otel.collector=${var.otel_collector},evals.engine=${var.evals_engine},skillspector.engine=${var.skillspector_engine},storage.className=gp3,storage.createAwsGp3=true"
}
output "ebs_csi_role_arn" { value = aws_iam_role.ebs_csi.arn }
