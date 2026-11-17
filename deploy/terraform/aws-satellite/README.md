# Ramen satellite cluster (AWS)

A worker-only EKS cluster in another region of the home account, with its own ALB and self-signed ACM certificate,
driven by the one console in the home cluster (CONTRACTS §28). Apply the home module (`../aws`) first; this one
shares its DynamoDB table, bucket, ECR and IAM roles and creates none of them. It does add one inline policy
(`ramen-satellite-<region>`) to the console role for this region's EKS, WAF and ALB ARNs, and an EKS access entry
mapping that role to the Kubernetes group `ramen-console`.

The console calls workers by pod IP, so the satellite's VPC is peered with the home one (§28.1): the module
creates the cross-region peering (accepted through a second `aws` provider in `home_region`), routes in both VPCs'
public route tables, and a rule on the satellite's cluster security group letting the home VPC CIDR in on tcp:8080.
Inputs come from the home apply: `home_vpc_id`, `home_vpc_cidr`, `home_route_table_ids` (outputs `vpc_id`,
`vpc_cidr`, `route_table_ids`); `vpc_cidr` (10.43.0.0/16) must not overlap the home 10.42.0.0/16.
As at home (D89), pods take their own subnets (10.43.64.0/20, 10.43.80.0/20, VPC CNI custom networking): add output
`alb_cidrs` to the home console's `RAMEN_AWS_ALB_CIDRS` so this cluster's ALB stays admitted by the worker policies.

```sh
cp terraform.tfvars.example terraform.tfvars   # region = the new region, home_vpc_id = terraform -chdir=../aws output -raw vpc_id
terraform init && terraform apply
$(terraform output -raw kubeconfig_command)
kubectl create ns ramen-system && kubectl label ns ramen-system ramen.io/routes=true
helm upgrade --install ramen-satellite ../../helm/ramen-satellite -n ramen-system $(terraform output -raw helm_set)
$(terraform output -raw host_command)           # the ALB hostname, a few minutes after the install
```

Then register it on the console: **Config → Clusters → Add satellite**, provider `aws`, the `region`, `cluster_name` and
that hostname. Full walk-through: `docs/wiki/deploy-aws.md`, "Satellite clusters". Keep one state per satellite.

Teardown: delete the satellite's zones in the console, `helm uninstall ramen-satellite -n ramen-system` (releases
the ALB), then `terraform destroy` here (the destroy sweeps leftover ENIs and the cluster security group as the home
module does).
