# deploy/ — running Ramen

| dir | what |
|---|---|
| `local/` | docker compose stack (Firestore emulator + console + one worker). `make demo` from the repo root. |
| `terraform/gcp/` | GCP infra per `docs/CONTRACTS.md` §7: GKE Autopilot (regional), Firestore Native, Artifact Registry, static IP, groups bucket, console GSA + Workload Identity. |
| `helm/ramen/` | console chart → namespace `ramen-system`: KSA `console` (Workload Identity + ClusterRole), Service, GKE **Gateway** `ramen` (global external HTTPS LB on the static IP, self-signed TLS Secret), HTTPRoute `/` → console, HealthCheckPolicy. |
| `helm/ramen-worker/` | one zone → namespace `ramen-<group>-<zone>` (labelled `ramen.io/routes=true`): `worker` + `worker-canary` Deployments pinned to a GCP zone, NEG Service, HTTPRoute `/mcp/<group>/<zone>` → `worker:8080/mcp` on the console Gateway, HealthCheckPolicy, KSA `worker`, Secret `ramen-deploy`. The console's gcp adapter renders/applies it; you can also apply it by hand. |
| `scripts/selfsigned.sh` | creates the console TLS Secret for an IP or host (D17). |
| `terraform/aws/` | **UNTESTED** AWS infra per `docs/CONTRACTS.md` §8: VPC (2 public subnets), EKS + one small managed node group, OIDC provider (IRSA), DynamoDB `ramen` table, S3 groups bucket, ECR `ramen/console` + `ramen/worker`, console IAM role, AWS Load Balancer Controller + Fluent Bit (CloudWatch Container Insights) via Helm, self-signed cert imported into ACM. |
| `cloudformation/ramen.yaml` | **UNTESTED** CloudFormation equivalent of the Terraform base (no Helm, no ACM import) for teams that cannot run Terraform. `cfn-lint` clean. |

## GCP bring-up (tested on a throwaway project, ~25 min, mostly GKE + LB provisioning)
Needs: gcloud (logged in), terraform ≥1.6, helm 4, kubectl, docker with buildx, `gke-gcloud-auth-plugin`.
```sh
export PROJECT=ramen-test-$(date +%y%m%d) REGION=us-central1
scripts/gcp_test_project.sh create                      # or use an existing project; billing must be linked
gcloud config set project $PROJECT
gcloud auth application-default login                   # terraform/gsutil creds (or: export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token))

# 1. infra
cd deploy/terraform/gcp && cp terraform.tfvars.example terraform.tfvars   # set project/region
terraform init && terraform apply                       # ~8 min; outputs cluster_name, region, console_ip, artifact_repo, console_gsa, groups_bucket
cd ../../..

# 2. images (linux/amd64 → Artifact Registry repo `ramen`)
make push PROJECT=$PROJECT REGION=$REGION               # runs gcloud auth configure-docker; ~5 min

# 3. console
gcloud container clusters get-credentials ramen --region $REGION
IP=$(terraform -chdir=deploy/terraform/gcp output -raw console_ip)
deploy/scripts/selfsigned.sh $IP                        # Secret ramen-system/ramen-console-tls
kubectl label ns ramen-system ramen.io/routes=true      # Gateway only admits routes from labelled namespaces
helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace \
  --set project=$PROJECT,region=$REGION \
  --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
  --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
kubectl -n ramen-system rollout status deploy/console
kubectl -n ramen-system get gateway ramen -w            # PROGRAMMED=True after ~5 min; ADDRESS = static IP
curl -k https://$IP/readyz                              # {"ok":true,...}
```
Console: `https://<console_ip>/` (self-signed, accept the warning), login `admin@ramen.local` / the password above.
Then in the console: add zone `a` (provider gcp, region `us-central1-a`), add group `demo` pointing at
`https://github.com/bkraad47/ramen-demo-mcp-group`, attach the zone, deploy. The console creates the
per-zone namespace, GSA and Secret; workers sync `gs://ramen-<project>-groups/<group>` on every reload.

### One zone by hand (no console; proves the worker path)
```sh
BUCKET=$(terraform -chdir=deploy/terraform/gcp output -raw groups_bucket)
git clone --depth 1 https://github.com/bkraad47/ramen-demo-mcp-group /tmp/demo && gsutil -m rsync -r -x '\.git/' /tmp/demo gs://$BUCKET/demo
gcloud iam service-accounts create ramen-demo-a
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:ramen-demo-a@$PROJECT.iam.gserviceaccount.com --role=roles/storage.objectViewer
gcloud iam service-accounts add-iam-policy-binding ramen-demo-a@$PROJECT.iam.gserviceaccount.com --role=roles/iam.workloadIdentityUser --member="serviceAccount:$PROJECT.svc.id.goog[ramen-demo-a/worker]"
helm template ramen-worker deploy/helm/ramen-worker --set project=$PROJECT,group=demo,zone=a,gcpZone=us-central1-a \
  --set secret.data.RAMEN_MCP_KEYS=rmk_$(openssl rand -hex 16),secret.data.RAMEN_ADMIN_KEY=$(openssl rand -hex 16) | kubectl apply -f -
kubectl -n ramen-demo-a rollout status deploy/worker        # first start: pod sync + pip install, 1-3 min
kubectl -n ramen-demo-a port-forward svc/worker 8080 &
curl -s localhost:8080/mcp -H "Authorization: Bearer <RAMEN_MCP_KEYS>" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
# same call through the LB once the Gateway has picked up the route (~2 min):
curl -sk https://$IP/mcp/demo/a -H "Authorization: Bearer <RAMEN_MCP_KEYS>" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```
MCP clients use `https://<console_ip>/mcp/<group>/<zone>` with `Authorization: Bearer rmk_…`. The LB backend service
for a zone is auto-named by GKE (`gkegw1-…-ramen-<group>-<zone>-worker-8080-…`); the console finds it by the NEG name
`ramen-<group>-<zone>` (worker Service `cloud.google.com/neg` annotation) for rebalance and Cloud Armor rules.

### Upgrade / redeploy the console
`make push PROJECT=… REGION=…` then `kubectl -n ramen-system rollout restart deploy/console && kubectl -n ramen-system rollout status deploy/console`.

## GCP teardown
```sh
helm uninstall ramen -n ramen-system                    # lets GKE release the Gateway's LB pieces first (optional, ~2 min)
terraform -chdir=deploy/terraform/gcp destroy           # cluster, bucket (force_destroy), Firestore, AR, IP, GSA
scripts/gcp_test_project.sh delete                      # RAMEN_GCP_PROJECT=<id>; deletes the whole project
```

## AWS bring-up — UNTESTED ON A REAL ACCOUNT
> **Nothing in this section has been applied to an AWS account** (none was available while building v0.3.0, F10.3 / D18).
> Every piece was checked only with `terraform validate`, `cfn-lint`, `helm lint`/`template` and unit tests against
> moto fakes. Expect rough edges the first time; please report them. The GCP path above is the verified reference.

Needs: aws CLI (logged in, `aws sts get-caller-identity` works), terraform ≥1.6, helm 4, kubectl, docker with buildx.
Mirror of the GCP flow: EKS instead of GKE Autopilot, DynamoDB instead of Firestore, S3 instead of GCS, Secrets Manager
instead of Secret Manager, one ALB (IngressGroup `ramen`, AWS Load Balancer Controller) instead of the GKE Gateway,
WAFv2 instead of Cloud Armor, IAM roles for service accounts (IRSA) instead of Workload Identity, CloudWatch Logs Insights
instead of Cloud Logging. Regional AWS API differences: the ALB cannot rewrite paths, so workers serve
`/mcp/<group>/<zone>` themselves (`RAMEN_MCP_PATH_PREFIX`); traffic splitting uses weighted target groups (stable/canary).
```sh
export AWS_REGION=us-east-1 ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# 1. infra (~15-20 min, mostly EKS). Terraform also installs the Load Balancer Controller + Fluent Bit via Helm.
cd deploy/terraform/aws && cp terraform.tfvars.example terraform.tfvars   # set region
terraform init && terraform apply                       # outputs cluster_name, ecr_console, ecr_worker, console_role_arn, certificate_arn, groups_bucket, ...
cd ../../..
#    CloudFormation alternative (base resources only; then install the two Helm charts by hand as in deploy/terraform/aws/helm.tf,
#    and import a self-signed cert: `aws acm import-certificate --certificate fileb://tls.crt --private-key fileb://tls.key`):
#    aws cloudformation deploy --stack-name ramen --template-file deploy/cloudformation/ramen.yaml --capabilities CAPABILITY_NAMED_IAM

# 2. images (linux/amd64 → ECR repos ramen/console, ramen/worker)
aws ecr get-login-password | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
docker buildx build --platform linux/amd64 -f node-rs/Dockerfile --build-arg VERSION=0.3.0 -t $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/ramen/worker:0.3.0 --push .
docker buildx build --platform linux/amd64 --build-arg RAMEN_VERSION=0.3.0 -t $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/ramen/console:0.3.0 --push console

# 3. console
aws eks update-kubeconfig --name ramen --region $AWS_REGION
CERT=$(terraform -chdir=deploy/terraform/aws output -raw certificate_arn)
helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace \
  --set provider=aws,region=$AWS_REGION --set-string aws.account=$ACCOUNT --set aws.certificateArn=$CERT \
  --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
  --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
kubectl -n ramen-system rollout status deploy/console
kubectl -n ramen-system get ingress console -w          # ADDRESS = ALB hostname after ~3 min
HOST=$(kubectl -n ramen-system get ingress console -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
curl -k https://$HOST/readyz                            # {"ok":true,"store":"dynamodb",...}
```
Console: `https://<ALB hostname>/` (self-signed, accept the warning), login `admin@ramen.local` / the password above.
The console runs with `RAMEN_STORE=dynamodb RAMEN_CLOUD=aws RAMEN_SECRETS_BACKEND=aws RAMEN_AWS_REGION RAMEN_GROUPS_BUCKET
RAMEN_IMAGE_WORKER RAMEN_EKS_CLUSTER RAMEN_ALB_GROUP=ramen` (all set by the chart). Then in the console: add zone `a`
(provider aws, region `us-east-1a` = the availability zone used as `nodeSelector`), add group `demo` pointing at
`https://github.com/bkraad47/ramen-demo-mcp-group`, attach the zone, deploy. The console creates the per-zone namespace
(Deployments `worker`/`worker-canary`, Services, ALB Ingress `/mcp/demo/a`, KSA), the IAM role `ramen-demo-a`
(path `/ramen/`, trusts the cluster OIDC provider for `ramen-demo-a/worker`, read-only on `s3://<bucket>/demo/` and
`ramen/demo/*` secrets) and Secret `ramen-deploy`; workers sync `s3://ramen-<account>-groups/<group>` on every reload.

### One zone by hand (no console)
```sh
BUCKET=$(terraform -chdir=deploy/terraform/aws output -raw groups_bucket)
git clone --depth 1 https://github.com/bkraad47/ramen-demo-mcp-group /tmp/demo && aws s3 sync /tmp/demo s3://$BUCKET/demo --exclude '.git/*' --delete
# IAM role for the worker KSA (the console does this in create_service_account); OIDC = terraform output oidc_provider_arn
helm template ramen-worker deploy/helm/ramen-worker --set provider=aws --set-string aws.account=$ACCOUNT \
  --set aws.region=$AWS_REGION,group=demo,zone=a,aws.zone=${AWS_REGION}a,aws.roleArn=<role arn> \
  --set secret.data.RAMEN_MCP_KEYS=rmk_$(openssl rand -hex 16),secret.data.RAMEN_ADMIN_KEY=$(openssl rand -hex 16) | kubectl apply -f -
kubectl -n ramen-demo-a rollout status deploy/worker
curl -sk https://$HOST/mcp/demo/a -H "Authorization: Bearer <RAMEN_MCP_KEYS>" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```
How the AWS adapter maps §7 features: `rebalance` rewrites the weighted forward action on the zone's Ingress (stable vs
canary target groups, canary share proportional to replicas, 0 when the canary is busy or down; `applied:false` + note
until the Load Balancer Controller has created the ALB). `ip-rules` writes WAFv2 IP sets `ramen-<group>` (+`-v6`) and a
rule in web ACL `ramen` that blocks `/mcp/<group>/` from other IPs (the ACL default is allow so the console path stays
reachable; the group's MCP path is effectively default-deny), patches `RAMEN_ALLOWED_CIDRS` into the deploy Secret,
rolls the workers, then associates the ACL with the ALB (retried in the background while the ALB is being created).
`logs` runs a Logs Insights query on `/aws/containerinsights/ramen/application` (Fluent Bit) filtered by namespace/pod.

## AWS teardown (untested)
```sh
helm uninstall ramen -n ramen-system                    # the controller deletes the ALB (wait ~2 min before destroy)
kubectl delete ns -l ramen.io/group                     # worker namespaces + their Ingresses
terraform -chdir=deploy/terraform/aws destroy           # EKS, VPC, bucket (force_destroy), table, ECR (force_delete), roles, cert
# leftovers the console created outside Terraform, if any: IAM roles under /ramen/, WAFv2 ip sets/web ACL `ramen`, secrets ramen/*
```
Estimated cost while running (us-east-1, on-demand): EKS control plane ~$0.10/h, 2× t3.small ~$0.04/h, ALB ~$0.025/h + LCU,
WAF web ACL ~$5/month + $1/rule, CloudWatch Logs ingestion, DynamoDB/S3/ECR at negligible usage. State is local
(`terraform.tfstate`, git-ignored).
Cost while running: the Autopilot cluster (pod-based billing, ~$0.05/h for console + 2 small workers, plus the
Autopilot control-plane fee after the free tier), the global HTTPS LB forwarding rule (~$0.025/h), a static IP,
Artifact Registry storage, Firestore/GCS at negligible usage. Terraform state is local (`terraform.tfstate`, git-ignored).
