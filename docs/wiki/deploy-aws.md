# Deploy on AWS

EKS, DynamoDB, S3, Secrets Manager, ECR and an application load balancer with gRPC target groups. The 0.6.0 run
applied this on a real account with two zones, the demo group, the Redis throttle, the bridge over TLS and a
teardown. Least exercised so far: WAF blocking under repeated rule changes, rebalance weights under real load and
Fluent Bit at volume. If a step breaks on your account, open an issue with the step and the error.

Cost while it runs: the EKS control plane (about $0.10 an hour while its Kubernetes version is in standard
support, six times that in extended support), four `t3.small` nodes (about $0.08 an hour), the load balancer
(about $0.025 an hour plus traffic) and CloudWatch logs.

## Prerequisites

`aws` CLI logged in with admin on the target account, `terraform` 1.6 or newer, `helm` 3.12 or newer, `kubectl`,
`docker` with buildx, `grpcurl`, `uv`, `openssl`, `python3`. SSO sessions expire mid-apply; `terraform apply`
resumes.

## 1. Infrastructure

```sh
cd deploy/terraform/aws && cp terraform.tfvars.example terraform.tfvars   # region, node size
terraform init && terraform apply        # about 20 minutes, nearly all of it EKS
cd ../../..
```

Terraform creates the cluster and node group, the DynamoDB table, the bucket, ECR repos, the console's IAM role,
a permissions boundary for the zone roles, a self-signed certificate imported into ACM, the AWS Load Balancer
Controller and Fluent Bit. A CloudFormation template with the same base resources is in `deploy/cloudformation`.

If the apply fails before its Helm releases, re-run it. Without the load-balancer controller the console never
gets an address.

## 2. Images

```sh
REGION=us-east-1; ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
EC=$(terraform -chdir=deploy/terraform/aws output -raw ecr_console); EW=$(terraform -chdir=deploy/terraform/aws output -raw ecr_worker)
docker buildx build --platform linux/amd64 --build-arg RAMEN_VERSION=$(cat VERSION) -t $EC:$(cat VERSION) --push console
docker buildx build --platform linux/amd64 -f node-rs/Dockerfile --build-arg VERSION=$(cat VERSION) -t $EW:$(cat VERSION) --push .
```

A push from a Colima or Lima Docker can fail with `broken pipe`. `docker save` plus `crane push` works.

## 3. The console

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
kubectl -n ramen-system get ingress console          # ADDRESS is the load balancer's DNS name after a few minutes
aws acm get-certificate --certificate-arn $CERT --region $REGION --query Certificate --output text > ramen-lb.pem
ALB=<that address>
curl --cacert ramen-lb.pem https://$ALB/readyz      # {"ok":true,"store":"dynamodb","version":"0.6.0"}
```

Then set the public address, which is the OAuth issuer the workers trust, and upgrade again:

```sh
helm upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values --set console.env.RAMEN_PUBLIC_URL=https://$ALB
```

The certificate is self-signed. Its name covers `*.<region>.elb.amazonaws.com`, so the exported PEM verifies the
load balancer for `curl --cacert` and the bridge's `--ca`.

## 4. The first zone, group and deploy

Open `https://<alb>/`, accept the certificate, and sign in as `admin@ramen.local`. The steps are the same as on
GCP: a zone named `a` with provider `aws` and region `us-east-1a`, the `demo` group from the demo repo, an
environment on zone `a`, a key, a canary deploy. Creating the environment makes the namespace `ramen-demo-a`, an
IAM role `ramen-demo-a` bound to the zone's service account through IRSA, and a zone Ingress with a gRPC target
group and the two header conditions. The load balancer needs a few minutes to program the listener rule.

```sh
curl --cacert ramen-lb.pem https://$ALB/mcp -H "Authorization: Bearer $RAMEN_MCP_KEY" -H 'Content-Type: application/json' \
  -H 'ramen-group: demo' -H 'ramen-zone: a' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
ramen-mcp-bridge --target $ALB:443 --tls --ca ramen-lb.pem --key "$RAMEN_MCP_KEY" --group demo --zone a
```

## The console's role and access controls

| It may | How |
|---|---|
| Create and delete the per-zone IAM roles | Only under the path `/ramen/`, and only within the permissions boundary Terraform created |
| Read and write the groups bucket | `GetObject`, `PutObject` and `DeleteObject` on its objects; `ListBucket` and `GetBucketLocation` on the bucket |
| Manage group secrets | Secrets Manager under `ramen/<group>/` |
| Use the state table and read logs | The `ramen` DynamoDB table, CloudWatch Logs Insights |
| Program edge IP rules | `wafv2` actions scoped to the web ACL `ramen` and its IP sets |
| Manage the zone namespaces | The same ClusterRole and per-zone Role as on GCP |

Each zone's workers assume `ramen-<group>-<zone>` through IRSA. The role reads the group's prefix of the bucket
and the group's secrets. Requested permissions add actions to that role's inline policy, named resources only when
a scope was given. [Users and access](users-access.md#service-account-permissions) covers the approval flow.

## Day 2

- **Scale, IP rules, logs** work as on GCP ([Groups, zones and regions](groups-zones.md#ip-rules)). Here an IP
  rule becomes a WAF IP set per group and a rule in the web ACL that blocks requests carrying
  `ramen-group: <group>` from any other source.
- **Node sizing**: worker pods are pinned to their zone's availability zone, and the node group spreads over the
  two subnets' zones. The defaults are `node_count = 4`, `node_max = 5`: two `t3.small` per availability zone. With
  one per zone, the first canary deploy stays `Pending` ("1 Insufficient memory"), because the node in `us-east-1a`
  also carries the console and CoreDNS. Scale later with `aws eks update-nodegroup-config`. A stuck rollout names
  the pending pod and the scheduler's reason in the deploy log.
- **Logs**: Fluent Bit ships every pod to the CloudWatch log group `/aws/containerinsights/ramen/application`.
  External monitors filter on `kubernetes.namespace_name = "ramen-<group>-<zone>"`.

## Teardown

```sh
# in the console: delete the group first, which removes its namespaces and IAM roles
helm uninstall ramen -n ramen-system                 # releases the load balancer
terraform -chdir=deploy/terraform/aws destroy
```

Check for leftovers: load balancers and target groups, the WAF web ACL and IP sets, CloudWatch log groups, the
ECR images, the ACM certificate, IAM roles under `/ramen/` and Secrets Manager entries under `ramen/`. If the
console was down when a group was deleted, its per-zone roles survive: `aws iam list-roles --path-prefix /ramen/`
lists them and `aws iam delete-role` removes each one once its inline policy is gone. The web ACL `ramen` is created by the console, not
by Terraform, and is billed monthly:

```sh
aws wafv2 delete-web-acl --scope REGIONAL --name ramen --id <id> --lock-token <token>
```

## Gotchas

- `curl -k` skips hostname verification; a real gRPC client does not. Export the certificate from ACM as above
  and pass it with `--ca`.
- The console's own range must be inside any IP lock, because a deploy smoke-tests the worker as an ordinary
  caller.
- The `Admin` gRPC service is not routed through the load balancer by design. A smoke test that tries it sees the
  load balancer's `464`.
