---
name: deploy-gcp
description: Bring Ramen up on a GCP project (GKE Autopilot, Firestore, GCS, Artifact Registry, global HTTPS LB) with Terraform + Helm, then create the first zone, group and deploy through the console API. Use when asked to deploy, install or stand up Ramen on GCP.
---

# Deploy Ramen on GCP

Verified path (v0.2.0+). Full narrative: `docs/how-tos/gcp.md`; binding contract: `docs/CONTRACTS.md` §7.
Takes ~25 min wall clock; most of it is GKE and load-balancer provisioning. Never skip waits.

## Inputs
- `PROJECT` (existing project with billing, or create one with `scripts/gcp_test_project.sh create`), `REGION` (default `us-central1`), first worker zone `GCP_ZONE` (default `us-central1-a`).
- Group repo URL to deploy (default `https://github.com/bkraad47/ramen-demo-mcp-group`).
- Tools on PATH: `gcloud` (logged in), `terraform >= 1.6`, `helm 4`, `kubectl`, `docker` + buildx, `gke-gcloud-auth-plugin`, `uv`, `openssl`, `python3`.

## Steps
1. Auth: `gcloud config set project $PROJECT && gcloud auth application-default login` (or export `GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)` for Terraform). Confirm billing is linked: `gcloud billing projects describe $PROJECT`.
2. Infra: `cd deploy/terraform/gcp && cp -n terraform.tfvars.example terraform.tfvars`, set `project`/`region`, `terraform init && terraform apply -auto-approve`. Record outputs `console_ip`, `public_hostname` (a free `sslip.io` hostname derived from `console_ip`, v0.5.5 — set `public_hostname` in `terraform.tfvars` instead if you have a real domain), `certificate_map`, `groups_bucket`, `artifact_repo`, `console_gsa`.
3. Images: `make push PROJECT=$PROJECT REGION=$REGION` (linux/amd64 → Artifact Registry `ramen/{console,worker}:<VERSION>`).
4. Console: `gcloud container clusters get-credentials ramen --region $REGION`; `kubectl label ns ramen-system ramen.io/routes=true --overwrite`; `HOSTNAME=$(terraform -chdir=deploy/terraform/gcp output -raw public_hostname)`, `CERTMAP=$(terraform -chdir=deploy/terraform/gcp output -raw certificate_map)`; generate `ADMIN_PW=$(openssl rand -base64 18)`, `FERNET=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')`, `ADMIN_KEY=$(openssl rand -hex 24)`; `helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace --set project=$PROJECT,region=$REGION --set gateway.certificateMap=$CERTMAP --set console.env.RAMEN_PUBLIC_URL=https://$HOSTNAME --set console.secrets.RAMEN_ADMIN_PASSWORD=$ADMIN_PW,console.secrets.RAMEN_FERNET_KEY=$FERNET,console.secrets.RAMEN_ADMIN_KEY=$ADMIN_KEY`. Hand the three secret values to the human once (they are not recoverable from the cluster without kubectl access) and do not print them again.
5. Wait: `kubectl -n ramen-system rollout status deploy/console`; poll `kubectl -n ramen-system get gateway ramen` until `PROGRAMMED=True` (up to 10 min); the managed cert reaches `ACTIVE` a few minutes after that (`gcloud certificate-manager certificates describe ramen-console`); `curl https://$HOSTNAME/readyz` must return `{"ok":true,...}` — no `-k`, the cert is publicly trusted.
6. First zone/group/env/key/deploy through the API (cookie login with `admin@ramen.local` / `$ADMIN_PW`): `POST /api/v1/zones {name:"a",provider:"gcp",region:"$GCP_ZONE"}`, `POST /api/v1/groups {name,repo_url,ref:"main"}`, `POST /api/v1/groups/<g>/environments {name:"default",ref:"main",zones:["a"]}`, `POST /api/v1/groups/<g>/mcp-keys {name:"first"}` (store the returned `rmk_` key for the validator), `POST .../environments/default/deploy {canary:true}` and poll `/api/v1/jobs/<id>` until `ok` (first run 1–3 min: bucket sync + pip install). Alternative: `scripts/cloud_smoke.sh https://$HOSTNAME admin@ramen.local "$ADMIN_PW" "$ADMIN_KEY"`.
7. Generate an `rmn_` API key for later automation: `POST /api/v1/api-keys {name:"ops",role:"super_admin"}`; give it to the human once.
8. Write a short runbook line into the ticket/PR: project, region, console hostname, zone, group, and the teardown commands below.

## Validate
(hand this section to a read-only sub-agent with `CONSOLE=https://<public_hostname>`, the `rmk_` key and a viewer `rmn_` key)
- V1 `curl -s $CONSOLE/readyz` → HTTP 200 and body contains `"ok":true` (no `-k`: the Google-managed cert is publicly trusted).
- V2 `curl -s -H "X-Ramen-Api-Key: $RMN" $CONSOLE/api/v1/groups/<g>/zones/a/workers` → HTTP 200, `live` has ≥1 item with `track:"stable"` and `load` in {low,even,high}.
- V3 (gRPC through the LB, no CA export needed — the cert is publicly trusted) `REQ=$(printf '%s' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | base64); grpcurl -import-path proto -proto ramen/v1/mcp.proto -H "authorization: Bearer $RMK" -H 'ramen-group: <g>' -H 'ramen-zone: a' -d "{\"body\":\"$REQ\"}" <public_hostname>:443 ramen.v1.Mcp/Call` → status OK and the base64-decoded `body` has `result.tools` non-empty.
- V4 Same call without the `authorization` header → gRPC status `Unauthenticated` (code 16).
- V5 `kubectl -n ramen-<g>-a get deploy worker-canary -o jsonpath='{.spec.replicas}'` → `0` or `1` (never more), and `kubectl -n ramen-<g>-a get deploy worker` READY ≥ 1/1.
- V6 `curl -sk -H "X-Ramen-Api-Key: $RMN" "$CONSOLE/api/v1/audit"` → contains an entry with `action` `deploy` and `ok:true`.

## Boundaries
- Never `terraform destroy`, delete namespaces, or delete the project unless the request explicitly says teardown; then use `docs/how-tos/gcp.md#teardown` and finish with `scripts/gcp_cost_check.sh $PROJECT --expect-empty`.
- Never print or store secret values, the Fernet key or keys beyond the one hand-over.
- Do not loop on `rebalance` / `ip-rules` responses with `applied:false` / `attached:false`; they retry in the background.
- Stop and ask a human if `terraform apply` fails on IAM/quota, if the Gateway is not programmed after 15 min, or if the deploy job reports pip errors from the group repo (that is the repo owner's problem, not infra).
