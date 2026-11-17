---
name: deploy-azure
description: Bring Ramen 1.0 up on Azure (AKS with workload identity and Calico network policies, Table Storage, blob storage, Key Vault, ACR in repository-permission mode, Azure Managed Redis, an Envoy Gateway edge with a Let's Encrypt certificate) with Terraform and Helm, deploy a first group, set up Microsoft Entra ID sign-in, verify it, and tear it all down cleanly. Use when asked to install, deploy, stand up, verify or tear down Ramen on Azure or AKS.
license: BSD-3-Clause
compatibility: Needs a checkout of github.com/bkraad47/ramen at the release tag, the az CLI (Owner, or Contributor plus User Access Administrator, on the subscription), terraform 1.6+, helm 3.14+, kubectl, curl, openssl, python3; docker buildx or crane only to push your own images.
metadata:
  version: "1.0.0"
  author: bkraad47
  docs: https://bkraad47.github.io/ramen/wiki/deploy-azure/
---

# Deploy Ramen on Azure

Source of truth: `docs/wiki/deploy-azure.md`, `docs/wiki/iam-network-azure.md` and `docs/wiki/sso.md` in the repo.
About 30 minutes; Terraform about fifteen.

## Rules
- Never print a secret. `terraform output helm_set` and `redis_url` are sensitive (they carry the Managed Redis
  key): expand them inline in the Helm command, never `echo` or paste them. Generate the three console secrets inline.
- Give this install its own kubeconfig: `export KUBECONFIG=$HOME/.kube/ramen-azure` before `az aks get-credentials`.
  A shared `~/.kube/config` keeps contexts of older clusters under the same names; a teardown run against a stale
  one leaves the load balancer behind and `terraform destroy` stalls on it.
- Pin the kube context: after `get-credentials`, `CTX=$(kubectl config current-context)`; `--context "$CTX"` /
  `--kube-context "$CTX"` on every call. Check `az account show --query name -o tsv` before applies and destroys.
- `terraform apply` is billable: plan, summarise, get the human's OK unless the request already said so.
- Stop and ask after three failures on a step, or on a quota or VM-size refusal you cannot fix from the message.

## Steps
1. **Subscription.** Register the providers once (a new subscription fails with `MissingSubscriptionRegistration`):
   `for ns in Microsoft.ContainerService Microsoft.Compute Microsoft.Network Microsoft.Storage Microsoft.KeyVault Microsoft.ManagedIdentity Microsoft.ContainerRegistry Microsoft.OperationalInsights; do az provider register -n $ns --wait; done`.
   Check quota: `az vm list-usage --location <location> -o table` (4 free cores in the node size's family).
2. **Infrastructure.** `cd deploy/terraform/azure && cp terraform.tfvars.example terraform.tfvars`: `subscription_id`,
   `location`; `node_size = "Standard_B2s_v2"` (the default; many subscriptions have no quota for it — `Standard_D2ls_v6` worked
   in the 1.0.0 runs; the error lists the sizes a subscription allows); `node_zones = []`
   when AKS answers `AvailabilityZoneNotSupported` (zones then are logical, `RAMEN_AZURE_ZONAL=0`); `acme_email`;
   engines as on the other clouds; `managed_redis = true` (default: Balanced_B0, TLS on 10000). `terraform init &&
   terraform plan -out=ramen.plan`, summarise, `terraform apply ramen.plan`; a `404` on the federated identity
   credential means Azure was slow: apply again. `cd ../../..`.
3. **Images.** Default: the public images (flags below, after `helm_set`). Own push: `az acr login --name <acr>`
   and push `ramen/console`, `ramen/worker`. The registry runs with repository permissions (ABAC): `AcrPush` given
   by hand does not count, and Terraform grants *Container Registry Repository Writer* on the platform repositories
   to the principal that ran it only — push as that principal (a `401` otherwise).
   crane: `az acr login --name <acr> --expose-token --query accessToken -o tsv | crane auth login <acr>.azurecr.io -u 00000000-0000-0000-0000-000000000000 --password-stdin`.
   With your own registry, push SkillSpector too and set `skillspector.image` (the chart's default is
   `ghcr.io/bkraad47/ramen-skillspector`, which must be pullable anonymously).
4. **Console.**
   ```sh
   tf() { terraform -chdir=deploy/terraform/azure "$@"; }; V=$(cat VERSION)
   $(tf output -raw kubeconfig_command) --file "$KUBECONFIG"; CTX=$(kubectl config current-context)
   kubectl --context "$CTX" create ns ramen-system
   kubectl --context "$CTX" label ns ramen-system ramen.io/routes=true
   helm --kube-context "$CTX" upgrade --install ramen deploy/helm/ramen -n ramen-system $(tf output -raw helm_set) \
     --set cluster.serviceCidr=10.0.0.0/16 \
     --set image.repository=docker.io/bkraad47/ramen-console --set image.tag=$V \
     --set console.env.RAMEN_IMAGE_WORKER=docker.io/bkraad47/ramen-worker:$V \
     --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
     --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
     --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
   ```
   `helm_set` carries the provider values, `RAMEN_PUBLIC_URL` (`https://<public_hostname>`), `builder.enabled=true`,
   `evals.clientId`, the engines and `redis.externalUrl`. `cluster.serviceCidr` (the AKS default range) is not in it
   and is what gives each zone its `worker-egress` policy (Calico enforces it). `tls.require` is always true (D86). With
   `otel_collector` on, spans flow once **Config → Telemetry** points at it.
5. **Wait.** `kubectl --context "$CTX" -n ramen-system rollout status deploy/console`;
   `kubectl --context "$CTX" -n ramen-system get certificate console` until `READY True` (a few minutes);
   `EDGE=$(tf output -raw public_hostname); curl -s https://$EDGE/readyz` → `"store":"azuretables"`.
   A certificate stuck `False`: `kubectl -n ramen-system describe challenge` (Let's Encrypt cannot reach port 80);
   `--set azure.tls=selfsigned` is the fallback (clients then need `--ca`).
6. **LLM provider, only if the group needs one** (LLM rails): the human adds Azure OpenAI or AI Foundry (key-based)
   on **Config → LLM providers**; the key stays in the console.
7. **First group**: [references/first-group.md](references/first-group.md) with `PROVIDER=azure`,
   `LOCATION=<location>-1` (needs a node in zone 1 unless `node_zones = []`).
8. **Entra ID sign-in (when asked)**: [references/entra.md](references/entra.md).
9. **Record** location, host, zone, group and the teardown pointer (no subscription id, no secrets).

Satellite clusters: [references/satellites.md](references/satellites.md).

## Validate
Read-only sub-agent with `U=https://<public_hostname>`, `CTX`, a viewer `rmn_` key and the group's `rmk_` key;
`curl` GETs/MCP calls and `kubectl get` only. Reply `PASS` or `FAIL: <id> — <evidence>`.
- V1 `curl -s $U/readyz` → 200, `"store":"azuretables"`, `"version":"1.0.0"`; no `-k` (Let's Encrypt).
- V2 `GET $U/api/v1/groups/demo/zones/<zone>/workers` (viewer key) → ≥ 1 live `stable` worker.
- V3 `tools/call` 2 + 3 through `$U/mcp` with the headers, key and configs `_meta` → `5`; without the key → 401.
- V4 `kubectl --context $CTX -n ramen-demo-<zone> get secret/ramen-tls networkpolicy/worker networkpolicy/worker-egress backendtlspolicy/worker` → present
  (Envoy verifies the workers against the install CA as `demo.<zone>.worker.ramen.internal`).
- V5 With `managed_redis` on: `kubectl --context $CTX -n ramen-system get secret ramen-redis` exists (the Managed
  Redis URL; never read it).

## Teardown
Only on an explicit request: [references/teardown.md](references/teardown.md). Groups and satellites first,
`helm uninstall` against `$CTX` **before** `terraform destroy` (the Envoy Service holds the static public IP), then
this skill's [scripts/leftovers.sh](scripts/leftovers.sh).
