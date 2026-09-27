# deploy/ — running Ramen

| dir | what |
|---|---|
| `local/` | docker compose stack (Firestore emulator + console + one worker). `make demo` from the repo root. |
| `terraform/gcp/` | GCP infra per `docs/CONTRACTS.md` §7: GKE Autopilot (regional), Firestore Native, Artifact Registry, static IP, groups bucket, console GSA + Workload Identity. |
| `helm/ramen/` | console chart → namespace `ramen-system`: KSA `console` (Workload Identity + ClusterRole), Service, GKE **Gateway** `ramen` (global external HTTPS LB on the static IP, self-signed TLS Secret), HTTPRoute `/` → console, HealthCheckPolicy. |
| `helm/ramen-worker/` | one zone → namespace `ramen-<group>-<zone>` (labelled `ramen.io/routes=true`): `worker` + `worker-canary` Deployments pinned to a GCP zone, NEG Service, HTTPRoute `/mcp/<group>/<zone>` → `worker:8080/mcp` on the console Gateway, HealthCheckPolicy, KSA `worker`, Secret `ramen-deploy`. The console's gcp adapter renders/applies it; you can also apply it by hand. |
| `scripts/selfsigned.sh` | creates the console TLS Secret for an IP or host (D17). |
| `terraform/aws/` | Phase 3 stub. |

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

## Teardown
```sh
helm uninstall ramen -n ramen-system                    # lets GKE release the Gateway's LB pieces first (optional, ~2 min)
terraform -chdir=deploy/terraform/gcp destroy           # cluster, bucket (force_destroy), Firestore, AR, IP, GSA
scripts/gcp_test_project.sh delete                      # RAMEN_GCP_PROJECT=<id>; deletes the whole project
```
Cost while running: the Autopilot cluster (pod-based billing, ~$0.05/h for console + 2 small workers, plus the
Autopilot control-plane fee after the free tier), the global HTTPS LB forwarding rule (~$0.025/h), a static IP,
Artifact Registry storage, Firestore/GCS at negligible usage. Terraform state is local (`terraform.tfstate`, git-ignored).
