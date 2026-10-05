# deploy/ — running Ramen

| dir | what |
|---|---|
| `local/` | docker compose stack (Firestore emulator + console + one worker). `make demo` from the repo root. |
| `terraform/gcp/` | GCP infra per `docs/CONTRACTS.md` §7: GKE Autopilot (regional), Firestore Native, Artifact Registry, static IP, groups bucket, console GSA + Workload Identity. |
| `helm/ramen/` | console chart → namespace `ramen-system`: KSA `console` (Workload Identity + ClusterRole), Service, GKE **Gateway** `ramen` (global external HTTPS LB on the static IP, Google-managed cert via Certificate Manager — `gateway.certificateMap`, v0.5.5 I11), HTTPRoute `/` → console, HealthCheckPolicy. |
| `helm/ramen-worker/` | one zone → namespace `ramen-<group>-<zone>` (labelled `ramen.io/routes=true`): `worker` + `worker-canary` Deployments pinned to a GCP zone, NEG Service (`appProtocol: kubernetes.io/h2c`), HTTPRoute on the console Gateway matching gRPC metadata `ramen-group`/`ramen-zone` (paths `/ramen.v1.Mcp`, `/grpc.health.v1.Health`, reflection; Admin stays internal), HealthCheckPolicy (GRPC), KSA `worker`, Secret `ramen-deploy`. The console's gcp adapter renders/applies it; you can also apply it by hand. |
| `scripts/selfsigned.sh` | fallback console TLS Secret for an IP or host (D17) — only needed if you unset `gateway.certificateMap` and go back to a self-signed cert. |
| `terraform/aws/` | AWS infra, verified live (v0.5.6 N10) per `docs/CONTRACTS.md` §8: VPC (2 public subnets), EKS + one small managed node group, OIDC provider (IRSA), DynamoDB `ramen` table, S3 groups bucket, ECR `ramen/console` + `ramen/worker`, console IAM role, AWS Load Balancer Controller + Fluent Bit (CloudWatch Container Insights) via Helm, self-signed cert imported into ACM. |
| `cloudformation/ramen.yaml` | **UNTESTED** CloudFormation equivalent of the Terraform base (no Helm, no ACM import) for teams that cannot run Terraform. `cfn-lint` clean. |

## Public images (0.7.0)
Every release publishes `ramen-worker` and `ramen-console` (linux/amd64 + linux/arm64, tags `<version>` and `latest`) to
`docker.io/bkraad47/` and `ghcr.io/bkraad47/`. Both charts default to the per-project registry (`make push` fills it),
but any image reference works on either cloud, so the push step is optional:
```sh
V=$(cat VERSION)
# console chart: console image + the worker image the console deploys (console.env wins over the computed default)
helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace ... \
  --set image.repository=docker.io/bkraad47/ramen-console --set image.tag=$V \
  --set console.env.RAMEN_IMAGE_WORKER=docker.io/bkraad47/ramen-worker:$V
# worker chart by hand:
helm template ramen-worker deploy/helm/ramen-worker --set image=docker.io/bkraad47/ramen-worker:$V ...
```
A group may also record a public reference as its worker image in the console (per-group images, CONTRACTS §13.3).
Swap `docker.io/` for `ghcr.io/` if Docker Hub is rate-limited from your cluster.

## GCP bring-up (tested on a throwaway project, ~25 min, mostly GKE + LB provisioning)
Needs: gcloud (logged in), terraform ≥1.6, helm 4, kubectl, docker with buildx, `gke-gcloud-auth-plugin`.
```sh
export PROJECT=ramen-test-$(date +%y%m%d) REGION=us-central1
scripts/gcp_test_project.sh create                      # or use an existing project; billing must be linked
gcloud config set project $PROJECT
gcloud auth application-default login                   # terraform/gsutil creds (or: export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token))

# 1. infra
cd deploy/terraform/gcp && cp terraform.tfvars.example terraform.tfvars   # set project/region
terraform init && terraform apply                       # ~8 min; outputs cluster_name, region, console_ip,
                                                          # public_hostname, certificate_map, artifact_repo, console_gsa, groups_bucket
cd ../../..

# 2. images (linux/amd64 → Artifact Registry repo `ramen`); optional — see "Public images" to pull docker.io/bkraad47/* instead
make push PROJECT=$PROJECT REGION=$REGION               # runs gcloud auth configure-docker; ~5 min

# 3. console
gcloud container clusters get-credentials ramen --region $REGION
HOSTNAME=$(terraform -chdir=deploy/terraform/gcp output -raw public_hostname)   # free sslip.io hostname (v0.5.5 I11); or your own domain if you set public_hostname
CERTMAP=$(terraform -chdir=deploy/terraform/gcp output -raw certificate_map)
kubectl label ns ramen-system ramen.io/routes=true      # Gateway only admits routes from labelled namespaces
helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace \
  --set project=$PROJECT,region=$REGION \
  --set gateway.certificateMap=$CERTMAP \
  --set console.env.RAMEN_PUBLIC_URL=https://$HOSTNAME \
  --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
  --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
kubectl -n ramen-system rollout status deploy/console
kubectl -n ramen-system get gateway ramen -w            # PROGRAMMED=True after ~5 min; ADDRESS = static IP
                                                          # the managed cert (kubectl get certificate -n ramen-system, if using the
                                                          # ManagedCertificate view, or `gcloud certificate-manager certificates describe
                                                          # ramen-console`) reaches ACTIVE a few minutes after the Gateway is programmed
curl https://$HOSTNAME/readyz                            # {"ok":true,...} — publicly-trusted cert, no -k needed
```
Console: `https://<public_hostname>/`, login `admin@ramen.local` / the password above. No certificate warning: the
cert is issued by a public CA, not self-signed.
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
grpcurl -plaintext -H 'authorization: Bearer <RAMEN_MCP_KEYS>' -d "{\"body\":\"$(printf '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | base64)\"}" localhost:8080 ramen.v1.Mcp/Call
# same call through the LB once the Gateway has programmed the route (2–7 min; routing by metadata, TLS at the LB,
# publicly-trusted cert so no -insecure needed once the managed cert is ACTIVE):
grpcurl -H 'ramen-group: demo' -H 'ramen-zone: a' -H 'authorization: Bearer <RAMEN_MCP_KEYS>' -d "{\"body\":\"$(printf '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | base64)\"}" $HOSTNAME:443 ramen.v1.Mcp/Call
```
MCP clients reach `<public_hostname>:443` over gRPC with metadata `ramen-group`/`ramen-zone` + `authorization: Bearer rmk_…`
(standard clients: `ramen-mcp-bridge --target <public_hostname>:443 --tls --key rmk_… --group demo --zone a` — no `--ca`/kubectl
step: the cert is publicly trusted, so the bridge verifies it with the system CA store like any other TLS client, see
[github.com/bkraad47/ramen-mcp-bridge](https://github.com/bkraad47/ramen-mcp-bridge)). The LB backend service
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
Cost while running: the Autopilot cluster (pod-based billing, ~$0.05/h for console + 2 small workers, plus the
Autopilot control-plane fee after the free tier), the global HTTPS LB forwarding rule (~$0.025/h), a static IP,
Artifact Registry storage, Firestore/GCS at negligible usage. Terraform state is local (`terraform.tfstate`, git-ignored).

## AWS bring-up — verified live on a real account (N10, 2026-10-03)
> `terraform apply` → images → `helm install` → zone/group/deploy → a real `tools/call` through the ALB, all
> on a throwaway EKS cluster. Found and fixed three bugs along the way (all now covered by tests/docs):
> `DynamoStore.from_env()` could raise `NoRegionError` under IRSA even with `AWS_REGION` set (botocore's
> default-session resolution, not visible with a plain `boto3.Session()` — fixed by passing `region_name`
> explicitly, matching `cloud/aws_api.py`'s existing pattern); the chart never wired `RAMEN_DDB_TABLE` at all
> (latent: only surfaces when the DynamoDB table name isn't literally `ramen`, now `aws.table` in values.yaml);
> and `aws.cluster`/`aws.table` must be set explicitly if your cluster/table isn't named `ramen` (the install
> command below now reads them straight from terraform's outputs instead of assuming the default matches).

Needs: aws CLI (logged in, `aws sts get-caller-identity` works), terraform ≥1.6, helm 4, kubectl, docker with buildx.
Mirror of the GCP flow: EKS instead of GKE Autopilot, DynamoDB instead of Firestore, S3 instead of GCS, Secrets Manager
instead of Secret Manager, one ALB (IngressGroup `ramen`, AWS Load Balancer Controller) instead of the GKE Gateway,
WAFv2 instead of Cloud Armor, IAM roles for service accounts (IRSA) instead of Workload Identity, CloudWatch Logs Insights
instead of Cloud Logging. The ALB routes gRPC by the `ramen-group`/`ramen-zone` header conditions to GRPC target groups (CONTRACTS §11); traffic splitting uses weighted target groups (stable/canary).

Needs: aws CLI (logged in, `aws sts get-caller-identity` works), terraform ≥1.6, helm 4, kubectl, docker with buildx.
Mirror of the GCP flow: EKS instead of GKE Autopilot, DynamoDB instead of Firestore, S3 instead of GCS, Secrets Manager
instead of Secret Manager, one ALB (IngressGroup `ramen`, AWS Load Balancer Controller) instead of the GKE Gateway,
WAFv2 instead of Cloud Armor, IAM roles for service accounts (IRSA) instead of Workload Identity, CloudWatch Logs Insights
instead of Cloud Logging. The ALB routes gRPC by the `ramen-group`/`ramen-zone` header conditions to GRPC target groups (CONTRACTS §11); traffic splitting uses weighted target groups (stable/canary).
```sh
export AWS_REGION=us-east-1 ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# 1. infra (~15-20 min, mostly EKS). Terraform also installs the Load Balancer Controller + Fluent Bit via Helm.
cd deploy/terraform/aws && cp terraform.tfvars.example terraform.tfvars   # set region
terraform init && terraform apply                       # outputs cluster_name, ecr_console, ecr_worker, console_role_arn, certificate_arn, groups_bucket, ...
cd ../../..
#    CloudFormation alternative (base resources only; then install the two Helm charts by hand as in deploy/terraform/aws/helm.tf,
#    and import a self-signed cert: `aws acm import-certificate --certificate fileb://tls.crt --private-key fileb://tls.key`):
#    aws cloudformation deploy --stack-name ramen --template-file deploy/cloudformation/ramen.yaml --capabilities CAPABILITY_NAMED_IAM

# 2. images (linux/amd64 → ECR repos ramen/console, ramen/worker); optional — see "Public images" to pull docker.io/bkraad47/* instead
aws ecr get-login-password | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
docker buildx build --platform linux/amd64 -f node-rs/Dockerfile --build-arg VERSION=0.3.0 -t $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/ramen/worker:0.3.0 --push .
docker buildx build --platform linux/amd64 --build-arg RAMEN_VERSION=0.3.0 -t $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/ramen/console:0.3.0 --push console

# 3. console
CLUSTER=$(terraform -chdir=deploy/terraform/aws output -raw cluster_name)
aws eks update-kubeconfig --name $CLUSTER --region $AWS_REGION
CERT=$(terraform -chdir=deploy/terraform/aws output -raw certificate_arn)
TABLE=$(terraform -chdir=deploy/terraform/aws output -raw dynamodb_table)
helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace \
  --set provider=aws,region=$AWS_REGION --set-string aws.account=$ACCOUNT --set aws.certificateArn=$CERT \
  --set aws.cluster=$CLUSTER --set aws.table=$TABLE \
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
(Deployments `worker`/`worker-canary`, Services, ALB Ingress with header conditions, KSA), the IAM role `ramen-demo-a`
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
grpcurl -insecure -H 'ramen-group: demo' -H 'ramen-zone: a' -H 'authorization: Bearer <RAMEN_MCP_KEYS>' -d "{\"body\":\"$(printf '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | base64)\"}" $HOST:443 ramen.v1.Mcp/Call
```
How the AWS adapter maps §7 features: `rebalance` rewrites the weighted forward action on the zone's Ingress (stable vs
canary target groups, canary share proportional to replicas, 0 when the canary is busy or down; `applied:false` + note
until the Load Balancer Controller has created the ALB). `ip-rules` writes WAFv2 IP sets `ramen-<group>` (+`-v6`) and a
rule in web ACL `ramen` that blocks `/mcp/<group>/` from other IPs (the ACL default is allow so the console path stays
reachable; the group's MCP path is effectively default-deny), patches `RAMEN_ALLOWED_CIDRS` into the deploy Secret,
rolls the workers, then associates the ACL with the ALB (retried in the background while the ALB is being created).
`logs` runs a Logs Insights query on `/aws/containerinsights/ramen/application` (Fluent Bit) filtered by namespace/pod.

## AWS teardown — verified live on a real account (N10, 2026-10-03)
```sh
helm uninstall ramen -n ramen-system                    # the controller deletes the ALB (wait ~2 min before destroy)
kubectl delete ns -l ramen.io/group                     # worker namespaces + their Ingresses
terraform -chdir=deploy/terraform/aws destroy           # EKS, VPC, bucket (force_destroy), table, ECR (force_delete), roles, cert
# leftovers the console created outside Terraform, if any: IAM roles under /ramen/, WAFv2 ip sets/web ACL `ramen`, secrets ramen/*
```
Live run found one real gap: the worker IRSA role the console creates (`ramen-demo-a` in this test, path `/ramen/`) sets
its permissions boundary to `worker_boundary` and attaches an inline policy, but nothing in `terraform destroy` or any
documented command tears it down — the boundary policy's `DeletePolicy` then fails with `DeleteConflict` since it's
still attached to that role. Fix until the console grows a teardown path for its own IAM objects: before
`terraform destroy`, delete each leftover role's inline policy and permissions boundary, then the role itself:
```sh
aws iam delete-role-policy --role-name <zone-role> --policy-name ramen-worker
aws iam delete-role-permissions-boundary --role-name <zone-role>
aws iam delete-role --role-name <zone-role>
```
A leftover Secrets Manager secret (`ramen/<group>/<env>/<zone>/mcp-*`) was also found this run — not cleaned up by
`terraform destroy` or helm uninstall, since the console, not Terraform, creates it. Delete explicitly:
`aws secretsmanager delete-secret --secret-id <name> --force-delete-without-recovery`.
Estimated cost while running (us-east-1, on-demand): EKS control plane ~$0.10/h, 2× t3.small ~$0.04/h, ALB ~$0.025/h + LCU,
WAF web ACL ~$5/month + $1/rule, CloudWatch Logs ingestion, DynamoDB/S3/ECR at negligible usage. State is local
(`terraform.tfstate`, git-ignored).
