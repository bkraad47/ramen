# Deploy on AWS (verbose) — **untested**

!!! danger "Built and unit-tested only"
    The AWS path (Terraform, CloudFormation, Helm `provider: aws`, console adapter, S3 sync) was written against
    [contract §8](../CONTRACTS.md) and tested with moto/fakes. **It has never been applied to a real AWS account**
    (the project had none). Expect to fix IAM details on first contact. Please open an issue with what broke.

## What it creates
Mirror of the GCP path with AWS primitives:

| Piece | AWS |
|---|---|
| Cluster | EKS `ramen`, one small managed node group (var `region`, default `us-east-1`) |
| Store | DynamoDB table `ramen` (pk/sk single table, on-demand) → `RAMEN_STORE=dynamodb` |
| Bucket | S3 `ramen-<account>-groups`, one prefix per group |
| Secrets | Secrets Manager `ramen/<group>/<env|all>/<zone|all>/<NAME>` → `RAMEN_SECRETS_BACKEND=aws` |
| Images | ECR `ramen/console`, `ramen/worker` |
| Identity | console IAM role via IRSA (S3, Secrets Manager, DynamoDB, EKS describe, IAM create-role and `PutRolePolicy` limited to path `/ramen/`, `wafv2` scoped to the `ramen` web ACL / IP sets, ELB); per group+zone IAM role `ramen-<group>-<zone>` trusting the cluster OIDC provider for KSA `ramen-<group>-<zone>/worker` |
| Edge | AWS Load Balancer Controller; one ALB (Ingress group `ramen`, HTTPS 443, self-signed cert imported into ACM); console Ingress `/`; per-zone Ingress with a **gRPC target group** (`alb.ingress.kubernetes.io/backend-protocol-version: GRPC`), listener conditions on headers `ramen-group` / `ramen-zone`, health check gRPC code `0` (`success-codes: "0"`) |
| Logs | Fluent Bit → CloudWatch Logs (Container Insights); console queries Logs Insights by namespace/pod |
| IP rules | WAFv2 IPSets `ramen-<group>` (+`-v6`) and a rule in web ACL `ramen` that blocks requests carrying `ramen-group: <group>` unless the source is in the set (the ACL default stays allow so the console path is reachable) + `RAMEN_ALLOWED_CIDRS` + worker roll |
| Rebalance | weighted target groups (stable/canary) via the Ingress `actions` annotation |

Since 0.3.1 there is no path to rewrite: the ALB routes on the two gRPC metadata headers and forwards HTTP/2 to
the worker port. gRPC target groups require an **HTTPS** listener on the ALB (the self-signed ACM cert satisfies
that) and the target group health check is a gRPC status (`0` = OK) against `grpc.health.v1.Health/Check`, so a
worker is out of rotation until its first successful load.

## 1. Infrastructure
=== "Terraform"
    ```sh
    aws configure                                    # or SSO; needs admin on the target account
    cd deploy/terraform/aws && cp terraform.tfvars.example terraform.tfvars   # region, cluster size
    terraform init && terraform apply                # ~20 min (EKS)
    cd ../../..
    ```
    Outputs: `cluster_name`, `region`, `console_url`, `ecr_console`, `ecr_worker`, `console_role_arn`.

=== "CloudFormation"
    ```sh
    aws cloudformation deploy --stack-name ramen --template-file deploy/cloudformation/ramen.yaml \
      --capabilities CAPABILITY_NAMED_IAM --region us-east-1
    ```
    Same base resources (EKS, node group, DynamoDB, S3, Secrets Manager, ECR, IAM roles, OIDC provider). The Helm
    steps below are documented, not templated.

## 2. Images
```sh
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin <account>.dkr.ecr.$REGION.amazonaws.com
docker buildx build --platform linux/amd64 -f node-rs/Dockerfile --build-arg VERSION=$(cat VERSION) -t <ecr_worker>:$(cat VERSION) --push .
docker buildx build --platform linux/amd64 --build-arg RAMEN_VERSION=$(cat VERSION) -t <ecr_console>:$(cat VERSION) --push console
```

## 3. Console
```sh
aws eks update-kubeconfig --name ramen --region $REGION
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
CERT=$(terraform -chdir=deploy/terraform/aws output -raw certificate_arn)     # or the ARN of the cert you imported into ACM
helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace \
  --set provider=aws,region=$REGION --set-string aws.account=$ACCOUNT --set aws.certificateArn=$CERT \
  --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
  --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
kubectl -n ramen-system rollout status deploy/console
kubectl -n ramen-system get ingress console          # ADDRESS = ALB DNS name after a few minutes
curl -k https://<alb-dns>/readyz
```
Console env on AWS: `RAMEN_STORE=dynamodb RAMEN_CLOUD=aws RAMEN_SECRETS_BACKEND=aws RAMEN_AWS_REGION
RAMEN_GROUPS_BUCKET RAMEN_IMAGE_WORKER RAMEN_EKS_CLUSTER RAMEN_ALB_GROUP=ramen` (all set by the chart from `region`, `aws.account`,
`aws.cluster`, `aws.albGroup` and `image.tag`; override with `console.env`).

## 4. Zone, group, deploy
Same as [GCP step 5](gcp.md#5-first-zone-group-and-deploy-console-or-api) with provider `aws` and region e.g.
`us-east-1a`. Clients target the ALB with gRPC and the routing metadata; export the ACM cert (or the PEM you
imported) for `--ca`:
```sh
ramen-mcp-bridge --target <alb-dns>:443 --tls --ca ramen-lb.pem --key rmk_… --group demo --zone a
grpc_health_probe -addr <alb-dns>:443 -tls -tls-ca-cert ramen-lb.pem -rpc-header 'ramen-group: demo' -rpc-header 'ramen-zone: a'
```
The raw `grpcurl` form is in [GCP step 6](gcp.md#6-call-it-through-the-load-balancer-grpc-routed-by-metadata)
(swap `$IP` for the ALB DNS name).

## Teardown
`helm uninstall ramen -n ramen-system` (releases the ALB), then `terraform -chdir=deploy/terraform/aws destroy` or
`aws cloudformation delete-stack --stack-name ramen`. Check for leftover ALBs, target groups, WAF ACLs and
CloudWatch log groups — these are the pieces most likely to survive a partial failure.

## Known unknowns
IRSA trust policies, ALB controller IAM, WAF association timing, CloudWatch Insights quotas, and (new in 0.3.1)
gRPC target groups with header-conditioned listener rules and a header-matched WAF rule were all written from
documentation, not from a run. Retry/backoff on ELB/WAF propagation is implemented as on GCP. Use the
[deploy-aws skill](../wiki/skills.md) which insists on a validator pass before calling the deployment done.
