---
name: deploy-gcp
description: Bring Ramen 1.0 up on Google Cloud (GKE Autopilot, Firestore, GCS, Artifact Registry, Secret Manager, global HTTPS load balancer with a Google-managed certificate) with Terraform and Helm, deploy a first group through the console API, verify it, and tear it all down cleanly. Use when asked to install, deploy, stand up, verify or tear down Ramen on GCP or GKE.
license: BSD-3-Clause
compatibility: Needs a checkout of github.com/bkraad47/ramen at the release tag, gcloud (logged in, billing linked) with gke-gcloud-auth-plugin, terraform 1.6+, helm 3.14+, kubectl, curl, openssl, python3; grpcurl and docker buildx are optional.
metadata:
  version: "1.0.0"
  author: bkraad47
  docs: https://bkraad47.github.io/ramen/wiki/deploy-gcp/
---

# Deploy Ramen on GCP

Source of truth: `docs/wiki/deploy-gcp.md` and `docs/wiki/iam-network-gcp.md` in the repo. Budget about 35 minutes:
Terraform about ten, then the Google-managed certificate turns `ACTIVE` 22–25 minutes after the apply. Poll; never
judge early.

## Rules
- Never print a secret: generate the admin password, Fernet key and admin key inline in the Helm command; read
  one back into a shell variable only when a step needs it (`PW=$(kubectl … | base64 -d)`), never `echo` it. Tell
  the human the `kubectl get secret` command instead of showing the value.
- Give this install its own kubeconfig: `export KUBECONFIG=$HOME/.kube/ramen-gcp` before `gcloud container clusters get-credentials`.
  A shared `~/.kube/config` keeps contexts of older clusters under the same names; a teardown run against a stale
  one leaves the load balancer behind and `terraform destroy` stalls on it.
- Pin the kube context. Right after `get-credentials`, `CTX=$(kubectl config current-context)`; pass
  `--context "$CTX"` to every `kubectl` and `--kube-context "$CTX"` to every `helm`. Another terminal or a satellite
  can switch the current context under you.
- `terraform apply` creates billable resources: show the plan summary and get the human's OK unless the request
  already said to create them. Teardown only on an explicit request.
- Stop and ask after three failed attempts at one step, on an IAM or quota error, or when the Gateway is not
  `PROGRAMMED` after 15 minutes.

## Steps
1. **Project.** `export RAMEN_GCP_PROJECT=<id> REGION=us-central1`. For a throwaway project:
   `RAMEN_BILLING_ACCOUNT=<id> scripts/gcp_test_project.sh create` (a deleted id stays reserved 30 days).
   `PROJECT=$RAMEN_GCP_PROJECT; gcloud config set project $PROJECT; gcloud auth application-default login`.
2. **Infrastructure.** `cd deploy/terraform/gcp && cp terraform.tfvars.example terraform.tfvars`; set `project`,
   `region`, and optionally `otel_collector` (`jaeger`|`phoenix`; spans flow only once **Config → Telemetry** points
   at it), `evals_engine = "promptfoo"`,
   `skillspector_engine = "skillspector"`. `terraform init && terraform plan -out="${TMPDIR:-/tmp}/ramen-gcp.plan"`, summarise, then
   `terraform apply "${TMPDIR:-/tmp}/ramen-gcp.plan"`; `cd ../../..`. With a token instead of ADC,
   `export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)` lasts an hour: refresh it before a long
   apply or destroy. Only a cluster created before 1.0.0c keeps `node_service_account = "default"`; drop that line
   for a fresh one (the nodes then run as `ramen-nodes@`).
3. **Images.** Default: the release's public images (step 4 flags). Own build instead: `make push PROJECT=$PROJECT
   REGION=$REGION` and drop the three image flags.
   With your own registry, push SkillSpector too and set `skillspector.image` (the chart's default is
   `ghcr.io/bkraad47/ramen-skillspector`, which must be pullable anonymously).
4. **Console.**
   ```sh
   gcloud container clusters get-credentials ramen --region $REGION --project $PROJECT
   CTX=$(kubectl config current-context); tf() { terraform -chdir=deploy/terraform/gcp "$@"; }; V=$(cat VERSION)
   EDGE=$(tf output -raw public_hostname)
   kubectl --context "$CTX" create ns ramen-system
   kubectl --context "$CTX" label ns ramen-system ramen.io/routes=true
   helm --kube-context "$CTX" upgrade --install ramen deploy/helm/ramen -n ramen-system \
     --set project=$PROJECT,region=$REGION --set gateway.certificateMap=$(tf output -raw certificate_map) \
     $(tf output -raw builder_set) --set evals.gsa=$(tf output -raw evals_gsa) $(tf output -raw engines_set) \
     --set cluster.serviceCidr=$(gcloud container clusters describe ramen --region $REGION --format='value(servicesIpv4Cidr)') \
     --set console.env.RAMEN_PUBLIC_URL=https://$EDGE \
     --set image.repository=docker.io/bkraad47/ramen-console --set image.tag=$V \
     --set console.env.RAMEN_IMAGE_WORKER=docker.io/bkraad47/ramen-worker:$V \
     --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
     --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
     --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
   ```
   `tls.require` is always true (D86); never try to set it false. `cluster.serviceCidr` is what gives every zone its
   `worker-egress` NetworkPolicy (GKE Dataplane V2 enforces it).
5. **Wait.** `kubectl --context "$CTX" -n ramen-system rollout status deploy/console`; poll
   `kubectl --context "$CTX" -n ramen-system get gateway ramen` until `PROGRAMMED=True`; then
   `curl -s https://$EDGE/readyz` until `{"ok":true,"store":"firestore","version":"<V>"}` (no `-k`). A `503
   unconditional drop overload` for a minute after a restart is normal.
6. **LLM provider, only if the group needs one.** A repo whose `mcp/guardrails.yaml` names LLM rails fails its
   deploy before any zone without a provider. Ask the human to add the key on **Config → LLM providers** (keys are
   write-only; never handle the key yourself unless they hand it to you for `PUT /api/v1/config/llm-providers`).
   The demo repo needs one: its `mcp/guardrails.yaml` names LLM rails.
7. **First group** through the API: follow [references/first-group.md](references/first-group.md) with
   `PROVIDER=gcp`, `LOCATION=us-central1-a`. Shortcut on a fresh console, the repo's
   `RAMEN_NODE_TLS=1 scripts/cloud_smoke.sh https://$EDGE admin@ramen.local "$PW" "$AK" $EDGE:443` with `PW`
   and `AK` read from Secret `ramen-console` (keys `RAMEN_ADMIN_PASSWORD`, `RAMEN_ADMIN_KEY`).
8. **Record** for the human: project, region, console URL, zone, group, the Helm values used (no secrets), and the
   teardown pointer below.

Satellite clusters in other regions: [references/satellites.md](references/satellites.md).

## Validate
Hand this to a read-only sub-agent with `U=https://<public_hostname>`, `CTX`, a viewer `rmn_` key (`RMN`) and the
group's `rmk_` key (`RMK`). It may run only `curl` GETs/MCP calls and `kubectl get`. Reply `PASS` or `FAIL: <id> — <evidence>`.
- V1 `curl -s $U/readyz` → 200, `"store":"firestore"`, `"version":"1.0.0"`; no `-k`.
- V2 `curl -s -H "X-Ramen-Api-Key: $RMN" $U/api/v1/groups/demo/zones/a/workers` → `live` has ≥ 1 worker with
  `track: stable`.
- V3 `tools/call` of `demo_calculator_tool` (2 + 3) on `$U/mcp` with `ramen-group`/`ramen-zone` headers, the key and
  the configs `_meta` ([references/first-group.md](references/first-group.md) step 6) → text `5`; the same without
  `Authorization` → HTTP 401.
- V4 `kubectl --context $CTX -n ramen-demo-a get secret/ramen-tls configmap/ramen-ca networkpolicy/worker networkpolicy/worker-egress` → all present
  (`worker`, and `worker-egress` when `cluster.serviceCidr` was set).
- V5 With an engine on: `kubectl --context $CTX -n ramen-system get secret ramen-engines` exists (never read it).

## Teardown
Only on an explicit request. Follow [references/teardown.md](references/teardown.md): groups and satellites first,
`helm uninstall` against `$CTX` **before** `terraform destroy`, then this skill's
[scripts/leftovers.sh](scripts/leftovers.sh) for what the console made outside Terraform.
