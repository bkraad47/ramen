# Deploy on GCP (verbose)

Verified end-to-end on a throwaway project (GKE Autopilot regional cluster, ~25 min wall clock, most of it GKE
and load-balancer provisioning). Binding details: [contract §7](../CONTRACTS.md). Cost while running: roughly the
Autopilot pods (~$0.05/h for console + two small workers, plus the control-plane fee after the free tier), the
global HTTPS LB forwarding rule (~$0.025/h), a static IP, Artifact Registry storage, negligible Firestore/GCS.

## Prerequisites
`gcloud` (logged in, billing linked), `terraform ≥ 1.6`, `helm 4`, `kubectl`, `docker` with buildx,
`gke-gcloud-auth-plugin` (`gcloud components install gke-gcloud-auth-plugin`), `uv`, `openssl`.

## 1. Project and credentials
```sh
export PROJECT=ramen-test-$(date +%y%m%d) REGION=us-central1
scripts/gcp_test_project.sh create        # throwaway project on your billing account; or use an existing one
gcloud config set project $PROJECT
gcloud auth application-default login     # Terraform/gsutil; or: export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
```

## 2. Infrastructure (Terraform, ~8 min)
```sh
cd deploy/terraform/gcp && cp terraform.tfvars.example terraform.tfvars   # set project, region
terraform init && terraform apply
cd ../../..
```
Creates: regional GKE Autopilot cluster `ramen`, Firestore Native `(default)`, Artifact Registry repo `ramen`,
static IP `ramen-console`, groups bucket `ramen-<project>-groups`, console GSA `ramen-console@<project>` with the
roles listed in §7, Workload Identity binding to `ramen-system/console`. Outputs: `cluster_name`, `region`,
`console_ip`, `artifact_repo`, `console_gsa`, `groups_bucket`. Nothing per-group is created by Terraform.

## 3. Images (~5 min)
```sh
make push PROJECT=$PROJECT REGION=$REGION
```
Builds `linux/amd64` images and pushes `<region>-docker.pkg.dev/<project>/ramen/{console,worker}:<VERSION>`.

## 4. Console (Helm)
```sh
gcloud container clusters get-credentials ramen --region $REGION
IP=$(terraform -chdir=deploy/terraform/gcp output -raw console_ip)
deploy/scripts/selfsigned.sh $IP                        # Secret ramen-system/ramen-console-tls (D17)
kubectl label ns ramen-system ramen.io/routes=true      # the Gateway only admits routes from labelled namespaces
helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace \
  --set project=$PROJECT,region=$REGION \
  --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
  --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
kubectl -n ramen-system rollout status deploy/console
kubectl -n ramen-system get gateway ramen -w            # PROGRAMMED=True after ~5 min; ADDRESS = static IP
curl -k https://$IP/readyz                              # {"ok":true,"store":"firestore","version":"0.3.0"}
```
Keep the three generated secrets somewhere safe (a password manager, not the shell history). The chart installs:
console Deployment (KSA `console`, Workload Identity), Service (NEG), Gateway `ramen`
(`gke-l7-global-external-managed`, static IP, TLS Secret), HTTPRoute `/`, HealthCheckPolicy, ClusterRole for
namespaces/deployments/secrets/services/HPAs/HTTPRoutes/pods, and a `GCPBackendPolicy` with a 300 s timeout.

Console env set by the chart: `RAMEN_STORE=firestore RAMEN_CLOUD=gcp RAMEN_SECRETS_BACKEND=gcp RAMEN_GCP_PROJECT
RAMEN_GCP_REGION RAMEN_GROUPS_BUCKET RAMEN_IMAGE_WORKER`.

## 5. First zone, group and deploy (console or API)
Open `https://<console_ip>/` (accept the self-signed warning), login `admin@ramen.local` / the password above.

1. **Zones → Add zone**: name `a`, provider `gcp`, region `us-central1-a` (the *GCP zone* the workers pin to).
2. **Groups → Add group**: name `demo`, repo `https://github.com/bkraad47/ramen-demo-mcp-group`, ref `main`.
3. **Groups → demo → Add environment**: name `default`, zones `a`. The console creates namespace `ramen-demo-a`,
   GSA `ramen-demo-a@<project>` (objectViewer on the group prefix, secretAccessor on `ramen-demo-*`) with
   Workload Identity, Service + NEG, HTTPRoute `/mcp/demo/a`, Secret `ramen-deploy`.
4. **Mint MCP key** on the group page (shown once) — this is what MCP clients send.
5. **Deploy (canary)**. Watch the job log: sync → canary → reload (first run: bucket sync + pip install, 1–3 min)
   → smoke → stable.

Same thing with the API (`rmn_` key or cookie):
```sh
C="curl -sk -H 'Content-Type: application/json' -H \"X-Ramen-Api-Key: $RMN\""
eval $C -X POST https://$IP/api/v1/zones -d "'{\"name\":\"a\",\"provider\":\"gcp\",\"region\":\"us-central1-a\"}'"
eval $C -X POST https://$IP/api/v1/groups -d "'{\"name\":\"demo\",\"repo_url\":\"https://github.com/bkraad47/ramen-demo-mcp-group\",\"ref\":\"main\"}'"
eval $C -X POST https://$IP/api/v1/groups/demo/environments -d "'{\"name\":\"default\",\"ref\":\"main\",\"zones\":[\"a\"]}'"
eval $C -X POST https://$IP/api/v1/groups/demo/mcp-keys -d "'{\"name\":\"first\"}'"        # → {"key":"rmk_…"}
eval $C -X POST https://$IP/api/v1/groups/demo/environments/default/deploy -d "'{\"canary\":true}'"   # → 202 {id}
```
Or run the whole thing with `scripts/cloud_smoke.sh https://$IP admin@ramen.local '<password>' '<admin key>'`
which prints PASS/FAIL.

## 6. Call it through the load balancer
```sh
curl -sk https://$IP/mcp/demo/a -H "Authorization: Bearer rmk_…" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
# {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"5"}],"isError":false}}
```
MCP clients use `https://<console_ip>/mcp/<group>/<zone>`. The Gateway rewrites the prefix to the worker's `/mcp`.
The route appears ~2 min after the namespace is created.

## 7. Day-2 from the console
- **Scale**: group page → zone row → count (admins) / size `s|m|l` + allowed sizes (super admin). HPA min = count.
- **IP rules**: CIDR list → Cloud Armor policy `ramen-<group>` on the zone's backend service + `RAMEN_ALLOWED_CIDRS`
  + worker roll. Empty list = allow all. `attached:false` with a note means the Gateway is still reconciling; the
  attach is retried in the background.
- **Rebalance**: capacity scaler per zone. See [Rebalance](../wiki/rebalance.md).
- **Logs**: Cloud Logging for the namespace (and one pod), downloadable.
- **Service account**: super admin creates/repairs the group+zone GSA; extra roles only through SA rules.
- **Refresh**: super admin re-discovers namespaces, deployments and GSAs into the store.
- **Secrets backend** is Secret Manager (`ramen-<group>-<env|all>-<zone|all>-<NAME>`).

## One zone by hand (no console; proves the worker path)
```sh
BUCKET=$(terraform -chdir=deploy/terraform/gcp output -raw groups_bucket)
git clone --depth 1 https://github.com/bkraad47/ramen-demo-mcp-group /tmp/demo && gsutil -m rsync -r -x '\.git/' /tmp/demo gs://$BUCKET/demo
gcloud iam service-accounts create ramen-demo-a
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:ramen-demo-a@$PROJECT.iam.gserviceaccount.com --role=roles/storage.objectViewer
gcloud iam service-accounts add-iam-policy-binding ramen-demo-a@$PROJECT.iam.gserviceaccount.com --role=roles/iam.workloadIdentityUser --member="serviceAccount:$PROJECT.svc.id.goog[ramen-demo-a/worker]"
helm template ramen-worker deploy/helm/ramen-worker --set project=$PROJECT,group=demo,zone=a,gcpZone=us-central1-a \
  --set secret.data.RAMEN_MCP_KEYS=rmk_$(openssl rand -hex 16),secret.data.RAMEN_ADMIN_KEY=$(openssl rand -hex 16) | kubectl apply -f -
kubectl -n ramen-demo-a rollout status deploy/worker
kubectl -n ramen-demo-a port-forward svc/worker 8080 &
curl -s localhost:8080/mcp -H "Authorization: Bearer <RAMEN_MCP_KEYS>" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Upgrade the console
`make push PROJECT=… REGION=…` then `kubectl -n ramen-system rollout restart deploy/console && kubectl -n ramen-system rollout status deploy/console`.

## Teardown
```sh
helm uninstall ramen -n ramen-system          # lets GKE release the Gateway's LB pieces first (~2 min)
terraform -chdir=deploy/terraform/gcp destroy # cluster, bucket (force_destroy), Firestore, AR, IP, GSA
scripts/gcp_test_project.sh delete            # RAMEN_GCP_PROJECT=<id>; deletes the whole project
scripts/gcp_cost_check.sh $PROJECT --expect-empty
```

## Gotchas we hit
- Gateway-managed backend services report "not ready" for minutes after a rollout: rebalance and IP rules return
  `applied:false` / `attached:false` with a note and retry in the background. Don't loop on them.
- The LB drains old pods for ~5 s after a rollout; MCP clients should retry once.
- The console GSA needs `roles/datastore.user` (Firestore) and the WI binding must depend on the cluster.
- `gcloud auth application-default login` is needed by Terraform even when `gcloud` is logged in.
