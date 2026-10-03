# Deploy on AWS, step by step

Applied to a real account since 0.5.6 (EKS, DynamoDB, S3, Secrets Manager, the ALB with gRPC target groups),
and the published `ramen-mcp-bridge` package was server-tested against it in 0.5.8. Each run was a throwaway
account state torn down the same day. Binding details: [contract §8](../CONTRACTS.md). Cost while running: the
EKS control plane (about $0.10/h), one small node group, the ALB (about $0.025/h plus traffic), CloudWatch logs.
DynamoDB on-demand, S3 and Secrets Manager are negligible at this scale.

!!! note "What the real-account runs did and did not cover"
    Covered (0.5.6, 0.5.8 and the 0.6.0 run): Terraform apply and destroy, images to ECR, the console on EKS behind
    the ALB on DynamoDB with Secrets Manager, two zones with IRSA roles, the demo group deployed with canary to
    both, `POST /mcp` through the ALB on both zones, the per-token throttle through an in-cluster Redis shared
    across zones, `mcp/env.yaml` rendered from a secret, the bridge over TLS with the ACM certificate, dropping a
    zone (namespace and IAM role gone within seconds), WAF IP sets created, teardown verified clean. Least
    exercised: WAF blocking under repeated rule changes (the group rule matched a path that no longer existed
    until 0.6.0 fixed it to match the `ramen-group` header), rebalance weights under real load, and Fluent Bit
    at volume. If something breaks on your account, please open an issue with the step and the error.

## Prerequisites
`aws` CLI (logged in with admin on the target account; SSO sessions expire, `terraform` resumes), `terraform ≥ 1.6`,
`helm 4`, `kubectl`, `docker` with buildx, `uv`, `openssl`, `python3`.

## What it creates
Mirror of the GCP path with AWS primitives:

| Piece | AWS |
|---|---|
| Cluster | EKS `ramen`, one small managed node group (var `region`, default `us-east-1`) |
| Store | DynamoDB table `ramen` (pk/sk single table, on-demand) → `RAMEN_STORE=dynamodb` |
| Bucket | S3 `ramen-<account>-groups`, one prefix per group |
| Secrets | Secrets Manager `ramen/<group>/<env\|all>/<zone\|all>/<NAME>` → `RAMEN_SECRETS_BACKEND=aws` |
| Images | ECR `ramen/console`, `ramen/worker` |
| Identity | console IAM role via IRSA; per group and zone an IAM role `ramen-<group>-<zone>` trusting the cluster OIDC provider for the zone's service account, under a permissions boundary |
| Edge | AWS Load Balancer Controller; one ALB (Ingress group `ramen`, HTTPS 443, self-signed cert imported into ACM); console Ingress `/`; per-zone Ingress with a gRPC target group and header conditions on `ramen-group` / `ramen-zone` |
| Logs | Fluent Bit → CloudWatch Logs (Container Insights); the console queries Logs Insights by namespace |
| IP rules | WAFv2 IP sets `ramen-<group>` (and `-v6`) and a rule in web ACL `ramen` that blocks requests carrying `ramen-group: <group>` unless the source is in the set, plus `RAMEN_ALLOWED_CIDRS` on the node |
| Rebalance | weighted target groups (stable/canary) via the Ingress `actions` annotation |

The console's IAM role may create roles and put role policies only under the path `/ramen/`, and its `wafv2`
rights are scoped to the `ramen` web ACL and IP sets.

Routing has no path to rewrite: the ALB matches the two headers and forwards to the zone's gRPC target group.
A gRPC target group needs an HTTPS listener (the ACM certificate satisfies that), and its health check is the
gRPC status `0` on `grpc.health.v1.Health/Check`, so a worker is out of rotation until its first successful load.
`POST /mcp` from HTTP/1.1 clients reaches the same target group through the ALB.

## 1. Infrastructure (Terraform, ~20 min)
=== "Terraform"
    ```sh
    aws configure                                    # or `aws sso login`; needs admin on the target account
    cd deploy/terraform/aws && cp terraform.tfvars.example terraform.tfvars   # region, cluster size
    terraform init && terraform apply                # ~20 min, nearly all of it EKS
    cd ../../..
    ```
    Outputs: `cluster_name`, `region`, `account`, `ecr_console`, `ecr_worker`, `console_role_arn`,
    `worker_boundary_arn`, `groups_bucket`, `dynamodb_table`, `certificate_arn`, `log_group`.

=== "CloudFormation"
    ```sh
    aws cloudformation deploy --stack-name ramen --template-file deploy/cloudformation/ramen.yaml \
      --capabilities CAPABILITY_NAMED_IAM --region us-east-1
    ```
    Same base resources (EKS, node group, DynamoDB, S3, Secrets Manager, ECR, IAM roles, OIDC provider). The Helm
    steps below are documented, not templated.

## 2. Images (~5 min)
```sh
REGION=us-east-1; ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
EC=$(terraform -chdir=deploy/terraform/aws output -raw ecr_console); EW=$(terraform -chdir=deploy/terraform/aws output -raw ecr_worker)
docker buildx build --platform linux/amd64 --build-arg RAMEN_VERSION=$(cat VERSION) -t $EC:$(cat VERSION) --push console
docker buildx build --platform linux/amd64 -f node-rs/Dockerfile --build-arg VERSION=$(cat VERSION) -t $EW:$(cat VERSION) --push .
```

## 3. Console (Helm)
```sh
aws eks update-kubeconfig --name ramen --region $REGION
CERT=$(terraform -chdir=deploy/terraform/aws output -raw certificate_arn)
TABLE=$(terraform -chdir=deploy/terraform/aws output -raw dynamodb_table)
helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace \
  --set provider=aws,region=$REGION --set-string aws.account=$ACCOUNT --set aws.cluster=ramen --set aws.table=$TABLE \
  --set aws.certificateArn=$CERT \
  --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
  --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
kubectl -n ramen-system rollout status deploy/console
kubectl -n ramen-system get ingress console          # ADDRESS = the ALB DNS name after a few minutes
aws acm get-certificate --certificate-arn $CERT --region $REGION --query Certificate --output text > ramen-lb.pem
ALB=<the ADDRESS above>
curl --cacert ramen-lb.pem https://$ALB/readyz      # {"ok":true,"store":"dynamodb","version":"0.6.0"}
```
Keep the three generated secrets somewhere safe. The certificate is self-signed and imported into ACM. Its SAN
covers `*.<region>.elb.amazonaws.com`, so the exported PEM verifies the ALB hostname for `curl --cacert` and the
bridge's `--ca`. If people will sign in through OAuth, add `--set console.env.RAMEN_PUBLIC_URL=https://$ALB` and
upgrade again: that URL is the issuer the workers trust.

Console env set by the chart: `RAMEN_STORE=dynamodb RAMEN_CLOUD=aws RAMEN_SECRETS_BACKEND=aws RAMEN_AWS_REGION
RAMEN_GROUPS_BUCKET RAMEN_IMAGE_WORKER RAMEN_EKS_CLUSTER RAMEN_ALB_GROUP=ramen` (from `region`, `aws.account`,
`aws.cluster`, `aws.albGroup` and `image.tag`; override with `console.env`).

## 4. First zone, group and deploy (console or API)
Open `https://$ALB/` (accept the self-signed certificate, or import `ramen-lb.pem`), login
`admin@ramen.local` / the password above.

1. **Zones → Add zone**: name `a`, provider `aws`, region `us-east-1a` (the availability zone the workers pin to).
2. **Groups → Add group**: name `demo`, repo `https://github.com/bkraad47/ramen-demo-mcp-group`, ref `main`.
3. **Groups → demo → Add environment**: name `default`, zones `a`. The console creates namespace `ramen-demo-a`,
   IAM role `ramen-demo-a` (read on the group's S3 prefix, `GetSecretValue` on `ramen/demo/*`) bound to the
   zone's service account through IRSA, a Role and RoleBinding for the console, a Service, a zone Ingress with a
   gRPC target group and the two header conditions, and Secret `ramen-deploy`.
4. **Generate key** on the group page (shown once). This is what MCP clients send.
5. **Deploy (canary)**. Watch the job log: sync → canary → reload (first run: bucket sync and pip install, 1–3 min)
   → smoke → stable. The ALB needs a few minutes to program the zone's listener rule; until then `/mcp` with
   the zone headers answers `404` from the console.

Same thing with the API (an `rmn_` key or the cookie):
```sh
C="curl -s --cacert ramen-lb.pem -H 'Content-Type: application/json' -H \"X-Ramen-Api-Key: $RMN\""
eval $C -X POST https://$ALB/api/v1/zones -d "'{\"name\":\"a\",\"provider\":\"aws\",\"region\":\"us-east-1a\"}'"
eval $C -X POST https://$ALB/api/v1/groups -d "'{\"name\":\"demo\",\"repo_url\":\"https://github.com/bkraad47/ramen-demo-mcp-group\",\"ref\":\"main\"}'"
eval $C -X POST https://$ALB/api/v1/groups/demo/environments -d "'{\"name\":\"default\",\"ref\":\"main\",\"zones\":[\"a\"]}'"
eval $C -X POST https://$ALB/api/v1/groups/demo/mcp-keys -d "'{\"name\":\"first\"}'"        # the key, shown once
eval $C -X POST https://$ALB/api/v1/groups/demo/environments/default/deploy -d "'{\"canary\":true}'"   # 202 {id}
```
Or run the whole thing with `scripts/cloud_smoke.sh https://$ALB admin@ramen.local '<password>' '<admin key>'`,
which prints PASS/FAIL per step.

## 5. Call it through the load balancer
The same ALB hostname serves the console and, by the `ramen-group` / `ramen-zone` headers, every zone's workers.

```sh
# Streamable HTTP, any client
curl --cacert ramen-lb.pem -X POST https://$ALB/mcp -H "Authorization: Bearer $RAMEN_MCP_KEY" \
  -H 'ramen-group: demo' -H 'ramen-zone: a' -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'

# stdio clients (Claude Desktop, Cursor): the bridge
ramen-mcp-bridge --target $ALB:443 --tls --ca ramen-lb.pem --key "$RAMEN_MCP_KEY" --group demo --zone a

# gRPC reachability (health only; the grpcurl form is below)
grpc_health_probe -addr $ALB:443 -tls -tls-ca-cert ramen-lb.pem -rpc-header 'ramen-group: demo' -rpc-header 'ramen-zone: a'
```
The `grpcurl` form is the one in [GCP step 6](gcp.md#6-call-it-through-the-load-balancer-grpc-routed-by-metadata)
with `-cacert ramen-lb.pem` and the ALB hostname. Signing in as a person instead of with a key:
[Connect an MCP client](mcp-clients.md).

## Day-2: external monitors (Datadog etc.)
No console needed. The `aws-for-fluent-bit` DaemonSet (installed by Terraform) tails every pod cluster-wide into
the CloudWatch Container Insights log group `/aws/containerinsights/<cluster>/application`. Point the monitor's
AWS integration at that log group and narrow to one zone the way the console does, with Logs Insights:
`filter kubernetes.namespace_name = "ramen-<group>-<zone>"`.

## Teardown
```sh
# in the console: delete the group (tears down its namespaces and IAM roles), then the zones
helm uninstall ramen -n ramen-system                 # releases the ALB
terraform -chdir=deploy/terraform/aws destroy        # or: aws cloudformation delete-stack --stack-name ramen
```
Check for leftovers afterwards: ALBs and target groups, the WAF web ACL and IP sets, CloudWatch log groups, IAM
roles under `/ramen/`, Secrets Manager entries under `ramen/`. These are the pieces most likely to survive a
partial failure. Deleting the group from the console first is what removes the per-zone roles and secrets. The
web ACL `ramen` is created by the console, not by Terraform, so `terraform destroy` leaves it behind (it is
billed monthly): `aws wafv2 delete-web-acl --scope REGIONAL --name ramen --id <id> --lock-token <token>`.

## Gotchas we hit
- **Hostname verification** (0.5.8): `curl -k` and `grpcurl -insecure` skip it, a real gRPC client does not. The
  certificate's SAN now covers the ALB hostname; export it from ACM as above rather than from the cluster.
- **Listener rule lag**: the ALB takes a few minutes to pick up a new zone Ingress. The console answers `404` on
  `/mcp` for that zone until then; the deploy job itself waits on the pod, not on the ALB.
- **SSO sessions expire** mid-apply. `terraform apply` is resumable; log in again and re-run it. If the apply
  failed before its Helm releases, the load-balancer controller is missing and the Ingress never gets an address:
  re-run the apply.
- **Node sizing**: worker pods are pinned to their zone's availability zone. Two `t3.small` nodes held one zone,
  not two; the 0.6.0 run needed four nodes (two per availability zone) for two zones with canaries. Since 0.6.0
  the canary rolls in place, so a zone needs room for its stable pods plus one canary, and a stuck rollout names
  the pending pod and its reason in the deploy log.
- **ECR pushes from a Colima or Lima Docker** can fail with `write: broken pipe` on one blob; `docker save` plus
  `crane push` from the host network worked.
- **The console's own range** must be inside any IP lock, because a deploy smoke-tests `tools/list` as an ordinary
  call through the node.
