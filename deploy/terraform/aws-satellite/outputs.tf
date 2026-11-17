output "cluster_name" { value = aws_eks_cluster.satellite.name }
output "region" { value = var.region }
output "account" { value = local.account }
output "certificate_arn" { value = aws_acm_certificate.edge.arn }
output "oidc_provider_arn" { value = aws_iam_openid_connect_provider.eks.arn }
output "console_role_arn" { value = local.console_role }
output "log_group" { value = aws_cloudwatch_log_group.application.name }
output "vpc_id" { value = aws_vpc.satellite.id }
output "vpc_cidr" { value = var.vpc_cidr }
# D89: this cluster's ALB subnets — append to the home console's RAMEN_AWS_ALB_CIDRS (deploy-aws.md, satellites)
output "alb_cidrs" { value = join(",", aws_subnet.public[*].cidr_block) }
output "pod_subnet_cidrs" { value = aws_subnet.pods[*].cidr_block }
output "peering_connection_id" { value = aws_vpc_peering_connection.home.id }
output "kubeconfig_command" { value = "aws eks update-kubeconfig --name ${aws_eks_cluster.satellite.name} --region ${var.region}" }
# The ALB is created by the Load Balancer Controller from the satellite chart's `edge` Ingress, so the host is only
# known after `helm install ramen-satellite`: this output tells you how to read it (the Clusters card's `host`).
output "host_command" {
  value = "kubectl -n ramen-system get ingress edge -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'"
}
# Everything `helm install ramen-satellite` needs, ready to paste (deploy-aws.md, satellite clusters)
output "helm_set" {
  value = join(" ", [
    "--set provider=aws,region=${var.region}",
    "--set aws.certificateArn=${aws_acm_certificate.edge.arn},aws.albGroup=${var.alb_group},aws.consoleGroup=${var.console_group}",
  ])
}
