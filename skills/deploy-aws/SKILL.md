---
name: deploy-aws
description: Bring Ramen 1.0 up on AWS (EKS with VPC CNI network policies and pod subnets, DynamoDB, S3, Secrets Manager, ECR, ALB with a self-signed ACM certificate, WAF) with Terraform and Helm, deploy a first group through the console API, verify it, and tear it all down cleanly. Use when asked to install, deploy, stand up, verify or tear down Ramen on AWS or EKS.
license: BSD-3-Clause
compatibility: Needs a checkout of github.com/bkraad47/ramen at the release tag, the aws CLI (admin on the target account), terraform 1.6+, helm 3.14+, kubectl, curl, openssl, python3; grpcurl, docker buildx or crane are optional.
metadata:
  version: "1.0.0"
  author: bkraad47
  docs: https://bkraad47.github.io/ramen/wiki/deploy-aws/
---

# Deploy Ramen on AWS

Source of truth: `docs/wiki/deploy-aws.md` and `docs/wiki/iam-network-aws.md` in the repo. Each release's cloud pass
applies this path on a real account. Budget about 30 minutes, most of it the EKS control plane and node group.

## Rules
- Never print a secret or an account id into chat, tickets or commits. Generate the three console secrets inline in
  the Helm command; read one back into a shell variable only when a step needs it; never `echo` it.
- Give this install its own kubeconfig: `export KUBECONFIG=$HOME/.kube/ramen-aws` before `aws eks update-kubeconfig`.
  A shared `~/.kube/config` keeps contexts of older clusters under the same names; a teardown run against a stale
  one leaves the load balancer behind and `terraform destroy` stalls on it.
- Pin the kube context: right after `update-kubeconfig`, `CTX=$(kubectl config current-context)`; pass
  `--context "$CTX"` / `--kube-context "$CTX"` everywhere. Check `aws sts get-caller-identity` is the intended
  account before every apply and destroy.
- `terraform apply` is billable: plan, summarise, get the human's OK unless the request already said so.
- Do not widen IAM to `*` to make something pass: report the missing action. Stop after three failures on a step.

## Steps
1. **Credentials.** `aws login --profile ramen` (or `aws sso login`), `export AWS_PROFILE=ramen AWS_REGION=us-east-1`.
   Terraform's AWS provider (5.x) cannot read a `login_session` profile: give it a wrapper profile in
   `~/.aws/config` (`credential_process = aws configure export-credentials --profile ramen --format process`) and
   `export AWS_PROFILE=<that profile>` for Terraform. The wrapper does not outlive the `aws login` session (about an
   hour; keys exported with `export-credentials` lapse sooner): log in again right before an apply (~25 min) or a
   destroy (~15 min) so it cannot expire half-way.
2. **Infrastructure.** `cd deploy/terraform/aws && cp terraform.tfvars.example terraform.tfvars` (region; defaults
   `node_count = 4`, `node_max = 5` `t3.small`; optional engines `otel_collector`, `evals_engine`,
   `skillspector_engine`). `terraform init && terraform plan -out="${TMPDIR:-/tmp}/ramen-aws.plan"`, summarise,
   `terraform apply "${TMPDIR:-/tmp}/ramen-aws.plan"`; `cd ../../..`. If it fails before its Helm releases (Load
   Balancer Controller, Fluent Bit), apply again. An apply that died while waiting on the node group leaves it
   *tainted* though AWS shows it `ACTIVE`: check `aws eks describe-nodegroup --cluster-name ramen --nodegroup-name
   ramen-podnet --query nodegroup.status`, then `terraform untaint aws_eks_node_group.ramen` (first
   `terraform import aws_eks_node_group.ramen ramen:ramen-podnet` if it is not in the state) before applying again —
   otherwise Terraform replaces it and the create fails on the existing name.
3. **Images.** Default: the public images (flags below). Own build: push to the `ecr_console` / `ecr_worker`
   outputs (`docs/wiki/deploy-aws.md` step 2; from Colima/Lima use `crane push`).
   With your own registry, push SkillSpector too and set `skillspector.image` (the chart's default is
   `ghcr.io/bkraad47/ramen-skillspector`, which must be pullable anonymously).
4. **Console.**
   ```sh
   tf() { terraform -chdir=deploy/terraform/aws "$@"; }; REGION=$(tf output -raw region); V=$(cat VERSION)
   export KUBECONFIG=$HOME/.kube/ramen-aws
   aws eks update-kubeconfig --name ramen --region $REGION; CTX=$(kubectl config current-context)
   helm --kube-context "$CTX" upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace \
     --set provider=aws,region=$REGION --set-string aws.account=$(tf output -raw account) \
     --set aws.cluster=ramen --set aws.table=$(tf output -raw dynamodb_table) \
     --set aws.certificateArn=$(tf output -raw certificate_arn) \
     $(tf output -raw builder_set) --set evals.roleArn=$(tf output -raw evals_role_arn) \
     $(tf output -raw engines_set) $(tf output -raw network_set) \
     --set cluster.serviceCidr=$(aws eks describe-cluster --name ramen --region $REGION --query cluster.kubernetesNetworkConfig.serviceIpv4Cidr --output text) \
     --set image.repository=docker.io/bkraad47/ramen-console --set image.tag=$V \
     --set console.env.RAMEN_IMAGE_WORKER=docker.io/bkraad47/ramen-worker:$V \
     --set redis.enabled=true \
     --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
     --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
     --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
   ```
   `network_set` gives the policies the ALB's public subnets (D89: pods live in their own subnets, so admitting the
   ALB admits no pod); without it the enforced policies cut the ALB off. `engines_set` carries the `gp3`
   StorageClass the engines' PVCs need. `tls.require` is always true (D86). Your own images: point `image.repository`
   and `RAMEN_IMAGE_WORKER` at the ECR repos, pin them with `image.digest` / `image.workerDigest`, and set
   `skillspector.image`. With `otel_collector` on, spans flow once **Config → Telemetry** points at it.
5. **Wait, then the public URL.** `kubectl --context "$CTX" -n ramen-system rollout status deploy/console`; poll
   `kubectl --context "$CTX" -n ramen-system get ingress console` until ADDRESS is set (`ALB=<it>`; if never, read
   `kubectl -n kube-system logs deploy/aws-load-balancer-controller`).
   `aws acm get-certificate --certificate-arn $(tf output -raw certificate_arn) --region $REGION --query Certificate --output text > ramen-lb.pem`;
   `curl --cacert ramen-lb.pem https://$ALB/readyz` → `"store":"dynamodb"`. Then
   `helm --kube-context "$CTX" upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values --set console.env.RAMEN_PUBLIC_URL=https://$ALB`
   (the OAuth issuer the workers trust).
6. **LLM provider, only if the group needs one** (LLM rails in `mcp/guardrails.yaml` — the demo repo has them): the
   human adds it on **Config → LLM providers**. Bedrock goes through the console's own role (`console_bedrock`, default on).
7. **First group**: [references/first-group.md](references/first-group.md) with `PROVIDER=aws`,
   `LOCATION=us-east-1a`, `CA=(--cacert ramen-lb.pem)`. Bridge clients need `--ca ramen-lb.pem`.
   A deploy stuck `Pending` with "Too many pods" or "Insufficient memory" needs nodes (`t3.small` holds 8 pods with
   pod subnets): raise `node_count`.
8. **Record** region, ALB host, zone, group and the teardown pointer (no account id, no secrets).

Satellite clusters: [references/satellites.md](references/satellites.md).

## Validate
Read-only sub-agent with `U=https://$ALB`, `ramen-lb.pem`, `CTX`, a viewer `rmn_` key and the group's `rmk_` key;
`curl` GETs/MCP calls and `kubectl get` only. Reply `PASS` or `FAIL: <id> — <evidence>`.
- V1 `curl --cacert ramen-lb.pem $U/readyz` → 200, `"store":"dynamodb"`, `"version":"1.0.0"`.
- V2 `GET $U/api/v1/groups/demo/zones/a/workers` (viewer key) → ≥ 1 live `stable` worker.
- V3 `tools/call` 2 + 3 through `$U/mcp` with the headers, key and configs `_meta` → `5`; without the key → 401.
- V4 `kubectl --context $CTX -n ramen-demo-a get secret/ramen-tls networkpolicy/worker networkpolicy/worker-egress` → present;
  `kubectl --context $CTX -n ramen-demo-a get pods -o wide` → pod IPs in the pod subnets (`10.42.64.0/20`,
  `10.42.80.0/20` by default), not the public `/24`s.
- V5 With engines on: `kubectl --context $CTX -n ramen-system get pvc` all `Bound`; Secret `ramen-engines` exists.

## Teardown
Only on an explicit request: [references/teardown.md](references/teardown.md). Groups and satellites first,
`helm uninstall` against `$CTX` **before** `terraform destroy` (it releases the ALB and the PVCs' EBS volumes), then
this skill's [scripts/leftovers.sh](scripts/leftovers.sh).
