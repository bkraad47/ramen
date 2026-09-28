output "cluster_name" { value = aws_eks_cluster.ramen.name }
output "region" { value = var.region }
output "account" { value = local.account }
output "ecr_console" { value = aws_ecr_repository.images["console"].repository_url }
output "ecr_worker" { value = aws_ecr_repository.images["worker"].repository_url }
output "console_role_arn" { value = aws_iam_role.console.arn }
output "worker_boundary_arn" { value = aws_iam_policy.worker_boundary.arn } # RAMEN_AWS_PERMISSIONS_BOUNDARY (console default matches)
output "groups_bucket" { value = aws_s3_bucket.groups.bucket }
output "dynamodb_table" { value = aws_dynamodb_table.state.name }
output "certificate_arn" { value = aws_acm_certificate.console.arn }
output "oidc_provider_arn" { value = aws_iam_openid_connect_provider.eks.arn }
output "log_group" { value = aws_cloudwatch_log_group.application.name }
output "kubeconfig_command" { value = "aws eks update-kubeconfig --name ${aws_eks_cluster.ramen.name} --region ${var.region}" }
# The ALB is created by the Load Balancer Controller from the console Ingress (helm chart), so its hostname is only
# known after `helm install ramen`: this output tells you how to read it.
output "console_url" {
  value = "https://$(kubectl -n ramen-system get ingress console -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
}
