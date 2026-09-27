# deploy/terraform/aws — Phase 3 stub
Untested, Phase 3 (v0.3.0). Planned shape mirrors GCP: EKS cluster, DynamoDB tables, one S3 bucket per
group, Secrets Manager, ALB + WAF IP sets for the per-group CIDR locks (G14), CloudWatch log groups.
No AWS account is available for testing (F10.3); contributions welcome.
