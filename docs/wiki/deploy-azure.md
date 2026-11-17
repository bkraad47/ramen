# Deploy on Azure

An AKS cluster with workload identity, Table Storage for the console's state, a blob container for the groups'
code, Key Vault for secrets, and an Envoy Gateway edge on a static public IP with a Let's Encrypt certificate on a
free `sslip.io` hostname. Since 0.7.5 ([CONTRACTS §21.6](../CONTRACTS.md)); the shape is the same as on GCP and
AWS: one cluster per install, and a namespace plus a managed identity per group and zone, made by the console.

!!! info "What has been run (0.7.5, 2026-10-11)"
    One install in `westus2` on two `Standard_B2s_v2` nodes with `node_zones = []`: the Let's Encrypt certificate was
    publicly trusted on the first try, the demo deploy and its edge route worked at once, the 33-check verification
    passed (NeMo rails, custom roles, tool access, Streamable HTTP and the bridge), Microsoft Entra ID sign-in with the
    groups lookup worked through the consent link, and the autoscaling load tests passed with no 5xx
    ([Autoscaling](autoscaling.md#load-tests)). Torn down afterwards.

Cost while it runs, cheap first: the AKS control plane is free (free tier), two `Standard_B2s_v2` nodes (about
$0.08 an hour together), a Standard public IP and load balancer rule (about $0.03 an hour), ACR Basic (about $0.17 a
day), and storage and Key Vault at cents. Tear it down when you are done.

## Prerequisites

`az` logged in to the subscription, `terraform` 1.6 or newer, `helm` 3.12 or newer, `kubectl` (`az aks
install-cli`), `docker` with buildx (or `crane`), `grpcurl`, `uv`, `openssl`, `python3`.

A new subscription has the resource providers unregistered, and AKS fails with `MissingSubscriptionRegistration`
until they are:

```sh
for ns in Microsoft.ContainerService Microsoft.Compute Microsoft.Network Microsoft.Storage Microsoft.KeyVault \
          Microsoft.ManagedIdentity Microsoft.ContainerRegistry Microsoft.OperationalInsights; do
  az provider register -n $ns --wait
done
az vm list-usage --location eastus -o table | grep -i "BS Family\|Total Regional"   # 4 B-series cores free at least
```

**Availability zones and VM sizes depend on the subscription.** Many subscriptions, new and trial ones especially,
get `AvailabilityZoneNotSupported ... The supported zones for location '<region>' are ''` from AKS even where plain
VMs list zones, and some refuse older sizes (`The VM size of Standard_B2s is not allowed in your subscription`). The
0.7.5 run met both in `eastus` and `westus2` and ran with:

```hcl
location   = "westus2"
node_size  = "Standard_B2s_v2"   # the default; the error message lists the sizes your subscription allows
node_max   = 2                   # a 4-core quota fits two 2-core nodes
node_zones = []                  # no availability zones for AKS in this subscription
```

With `node_zones = []` Terraform sets `azure.zonal=false`, the console gets `RAMEN_AZURE_ZONAL=0`, and a Ramen zone
on Azure (`westus2-1`) is a logical placement: its workers run on any node instead of being pinned to an Azure
availability zone. Where AKS does offer zones, leave `node_zones = ["1"]` (add `"2"`, `"3"` for more Ramen zones).

Whoever runs Terraform needs Owner on the subscription (or Contributor plus User Access Administrator): Terraform
creates role assignments for the console's identity.

## 1. Infrastructure

```sh
cd deploy/terraform/azure && cp terraform.tfvars.example terraform.tfvars   # subscription_id; location (eastus)
terraform init && terraform apply
cd ../../..
```

Terraform creates the resource group `ramen-<suffix>`, the AKS cluster (OIDC issuer and workload identity on, Azure
CNI overlay with Calico enforcing NetworkPolicies, one node pool in the zones of `node_zones`, or none), ACR, the storage account with the `groups` container and the `ramen` table,
Key Vault in RBAC mode, the edge's static public IP, the console's user-assigned identity with its federated
credential for `ramen-system/console` (it creates each group's build identity, D85), an Azure Managed Redis
(`managed_redis`, on by default: `Balanced_B0`, no HA, TLS on port 10000, access-key auth — the shared Redis every zone
gets for the scope throttle and the task store, [Throttling](throttling.md)), and installs Envoy Gateway and cert-manager
into the cluster. About fifteen minutes. `terraform output helm_set` prints every Helm value the next steps need; it
carries the Redis key (`redis.externalUrl`), so the output is sensitive — `-raw` prints it all the same.

If the first apply stops with a `404` on the federated identity credential, Azure had not finished creating the
identity: run `terraform apply` again (the 0.7.5 run needed exactly that second apply).

## 2. Images

```sh
ACR=$(terraform -chdir=deploy/terraform/azure output -raw acr); V=$(cat VERSION)
az acr login --name ${ACR%%.*}
docker buildx build --platform linux/amd64 --build-arg RAMEN_VERSION=$V -t $ACR/ramen/console:$V --push console
docker buildx build --platform linux/amd64 -f node-rs/Dockerfile --build-arg VERSION=$V -t $ACR/ramen/worker:$V --push .
```

From a Colima or Lima Docker, `docker save` the images and push the tarballs with `crane push` after
`az acr login --name <acr> --expose-token` (`crane auth login $ACR -u 00000000-0000-0000-0000-000000000000 -p <token>`).
Or skip the build: the release's public images `docker.io/bkraad47/ramen-console` and `ramen-worker` (also on
`ghcr.io/bkraad47/`) run with `--set image.repository=docker.io/bkraad47/ramen-console --set image.tag=$V
--set console.env.RAMEN_IMAGE_WORKER=docker.io/bkraad47/ramen-worker:$V` added after the Terraform values in the Helm
command below.

## 3. The console

```sh
$(terraform -chdir=deploy/terraform/azure output -raw kubeconfig_command)
kubectl create ns ramen-system
kubectl label ns ramen-system ramen.io/routes=true      # the Gateway's 443 listener admits labelled namespaces only
helm upgrade --install ramen deploy/helm/ramen -n ramen-system \
  $(terraform -chdir=deploy/terraform/azure output -raw helm_set) \
  --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
  --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
kubectl -n ramen-system rollout status deploy/console
HOSTNAME=$(terraform -chdir=deploy/terraform/azure output -raw public_hostname)
kubectl -n ramen-system get certificate console      # READY True within a minute or two
curl https://$HOSTNAME/readyz                        # {"ok":true,"store":"azuretables","version":"…"}
```

The chart sets `RAMEN_CLOUD=azure`, `RAMEN_STORE=azuretables` and `RAMEN_SECRETS_BACKEND=azure`, points the console
at the storage account and Key Vault, gives it the workload identity, and sets `RAMEN_PUBLIC_URL` to
`https://<public_hostname>`, the OAuth issuer the workers trust. The certificate comes from Let's Encrypt through an
HTTP-01 challenge on the Gateway's port 80, which serves nothing else but a redirect to https. Where Let's Encrypt
cannot reach the IP, `--set azure.tls=selfsigned` issues a self-signed certificate instead (clients then need
`--ca` / `--cacert`, as on AWS); `--set azure.acmeServer=https://acme-staging-v02.api.letsencrypt.org/directory`
tests the flow against the staging CA first.

`helm_set` includes `builder.enabled=true`. It is what lets a deploy build a group's worker image from its
`requirements.txt` into the group's own repository `ramen/groups/<group>/worker`, as the group's own identity
([Worker images](worker-images.md#per-group-images-100c-d85)). Without it no image is ever built: workers install
requirements when they load. Add `--set image.digest=sha256:…,image.workerDigest=sha256:…` to run the platform images
by digest.
It also carries `evals.clientId` (output `evals_client_id`), the `ramen-evals` identity the evals and scan Jobs run
as: it has no role assignment at all, so a group's promptfoo config cannot reach the registry, the storage account
or Key Vault.
`helm_set` does not carry `cluster.serviceCidr`: without it the console writes no `worker-egress` NetworkPolicy into
the zone namespaces (the chart's NOTES warn). Add `--set cluster.serviceCidr=10.0.0.0/16` (the AKS default Service
range; Terraform sets no other) to get it.

**Optional engines.** `otel_collector = "jaeger" | "phoenix"`, `evals_engine = "promptfoo"` and
`skillspector_engine = "skillspector"` in `terraform.tfvars` (all `none` by default) become the chart's
`otel.collector`, `evals.engine` and `skillspector.engine`; `helm_set` carries them (also on their own as
`terraform output -raw engines_set`): a try-out trace UI ([Telemetry](telemetry.md)), the promptfoo server every
evals run is shared with ([Evals](evals.md)), and SkillSpector scans of every group's skills
([SkillSpector](skillspector.md)). Each lives in `ramen-system` without auth or an edge route (`kubectl port-forward`);
turning one on changes only that engine's objects.

## 4. The first zone, group and deploy

Sign in at `https://<public_hostname>/` as `admin@ramen.local`, then the same steps as on GCP: a zone with
provider `azure` and region `eastus-1` (Ramen names Azure zones `<location>-<n>`; with `node_zones` set, the node
pool must have a node in that zone, zone 1 by default; with `node_zones = []` any name of the location works, the
0.7.5 run used `westus2-1`), the `demo` group from the demo repo, an environment on that zone, an MCP key, a
canary deploy. Creating the environment makes the namespace `ramen-demo-<zone>`, a user-assigned identity
`ramen-demo-<zone>` with a federated credential for the namespace's `worker` service account, its role assignments
(blob reader on the `groups` container, Key Vault Secrets User on exactly the group's secrets), and an HTTPRoute on
the Gateway matching the two headers. Each deploy also writes the zone's TLS leaf (Secret `ramen-tls`, ConfigMap
`ramen-ca`) and a `BackendTLSPolicy` on Service `worker`, so Envoy verifies the workers against the install CA as
`worker.ramen.internal` (D86).

```sh
curl https://$HOSTNAME/mcp -H "Authorization: Bearer $RAMEN_MCP_KEY" -H 'Content-Type: application/json' \
  -H 'ramen-group: demo' -H 'ramen-zone: eastus-1' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
ramen-mcp-bridge --target $HOSTNAME:443 --tls --key "$RAMEN_MCP_KEY" --group demo --zone eastus-1
```

The demo repo ships `mcp/llm-config/`, so the workers refuse a call that does not carry the accepted configs
version (`-32028 configs required`): add `"_meta": {"io.ramen/configs": {"version": "<v>"}}` to `params`, with `<v>` from
`/.well-known/ramen-policy?group=demo&zone=<zone>` ([Deploy your first group → Call a tool](../first-group.md#8-call-a-tool)). The bridge
does this by itself.

`scripts/cloud_smoke.sh` does the whole step: `RAMEN_NODE_TLS=1 RAMEN_ZONE_PROVIDER=azure scripts/cloud_smoke.sh
https://$HOSTNAME admin@ramen.local <password> <admin key> $HOSTNAME:443`.

Sign-in through Microsoft Entra ID or Google works exactly as on the other clouds ([Entra ID and
Workspace](sso.md)); register `https://<public_hostname>/auth/<provider>/callback` on the app.

## The console's identity and access controls

Every identity and network path in one place: [IAM and network on Azure](iam-network-azure.md).

| It may | How |
|---|---|
| Sync group repos into the `groups` container | Storage Blob Data Contributor on the resource group |
| Keep its own state | Storage Table Data Contributor (table `ramen`) |
| Manage group secrets | Key Vault Secrets Officer; secrets are named `ramen--<group>--<env>--<zone>--<VAR>` (`all` where the secret is not scoped) |
| Create and delete zone identities | Managed Identity Contributor (identities and their federated credentials) |
| Grant a zone identity its roles | Role Based Access Control Administrator, used only for the permission catalogue's roles |
| Make each group's build identity (D85) | The two roles above: managed identity `ramen-build-<group>` (tag `ramen-build-group`) federated with `ramen-system/ramen-build-<group>`, assigned `Container Registry Repository Writer` on the registry under an ABAC condition `@Request[Microsoft.ContainerRegistry/registries/repositories:name] StringEqualsIgnoreCase 'ramen/groups/<group>/worker'` (and `… Reader` on the worker base repository) |
| Call a model for LLM rails (§38.1) | Nothing to grant: Azure OpenAI and AI Foundry providers are key-based, and the key stays in the console (its judge proxy makes the calls; workers never hold it) |
| Remove a deleted group's repository | A custom data-plane role (`metadata/read`, `metadata/delete`, `content/delete` — no write) on the registry, conditioned to repositories starting with `ramen/groups/`; the console exchanges its Entra token for a registry token scoped to that one repository |
| Manage the zone namespaces and routes | The chart's ClusterRole, the same as on GCP, plus Envoy Gateway `SecurityPolicy` / `BackendTrafficPolicy` and Gateway API `BackendTLSPolicy` (D86) |

Every role is scoped to the install's resource group; nothing is granted on the subscription. A blob role cannot be
scoped to a path by its *scope* (the finest scope is the container), only by an ABAC *condition* on the assignment:
the console writes one on each zone identity's `Storage Blob Data Reader` (`conditionVersion 2.0`), so it reads only
blobs whose path starts with `<group>/` (`@Resource[...containers/blobs:path]`) and lists only that prefix
(`@Request[...containers/blobs:prefix]`); the storage account is general-purpose v2, which conditions need. Secrets are
scoped by one `Key Vault Secrets User` assignment per secret.

The registry runs with repository permissions (`role_assignment_mode = "AbacRepositoryPermissions"`), where ACR
honours the `Container Registry Repository *` roles and their conditions, not `AcrPull` / `AcrPush`. The other
identities Terraform creates: the cluster's kubelet identity with `Container Registry Repository Reader` on the
registry (nodes; every repository), and `ramen-evals` with nothing. There is no shared builder identity any more: no
build identity can write the platform repositories `ramen/console`, `ramen/worker`, `ramen/skillspector`. The
residual: the console's Role Based Access Control Administrator could, if the console were compromised, assign itself
a registry role — the console is the platform's crown jewel on every cloud ([Threat model](../threat-model.md)).

## Day 2

- **Upgrading to TLS on every hop (D86) from 1.0.0b/c**: `helm upgrade … --reset-then-reuse-values`, wait for the
  console and the chart services to restart on TLS, then **redeploy every zone** — until a zone is redeployed the
  console cannot reach it and its wake, judge, telemetry and Redis calls fail, though it still serves clients. The
  exact steps: [Configuration → Upgrading the chart](configuration.md#upgrading-the-chart).
- **IP rules**: a CIDR list per zone on the group page. The node enforces it (authoritative, as everywhere) and the
  console mirrors it at the edge as an Envoy Gateway `SecurityPolicy` on the zone's route. The Envoy Service keeps
  the client's address (`externalTrafficPolicy: Local`), so workers trust one forwarded hop.
- **Logs**: read from the pods through the Kubernetes API; no Log Analytics workspace is needed.
- **Upgrade the console**: push the images, then `kubectl -n ramen-system rollout restart deploy/console`. The
  rollout keeps one ready pod and drains the old one for 40 s, so the edge never answers without a backend.
- **Upgrading a cluster from before 1.0.0c: the NetworkPolicy engine.** The modules now set
  `network_policy = "calico"` (home and satellite), so AKS enforces the policies Ramen writes. On a cluster created
  without it, run `terraform plan` first: if it shows the cluster `must be replaced`, either accept a new cluster
  (the state, groups, secrets and images live outside it: re-run step 3 and redeploy every zone) or turn Calico on in
  place first where your AKS version offers it (`az aks update -g <rg> -n ramen --network-policy calico`), after
  which the plan shows no change to the cluster.
- **Scaling**: set a max per zone on the group page and turn on the scheduler on the Config page; Ramen scales the
  workers between 1 and the max, and the AKS cluster autoscaler (`node_max`) adds nodes for them
  ([Autoscaling](autoscaling.md)).

## Satellite clusters (other regions)

A zone must be in a location that has a cluster ([where a zone can be](groups-zones.md#where-a-zone-can-be)). For
a zone in another location, add a **satellite**: a worker-only AKS cluster there, in the same subscription, with its
own static public IP and Envoy Gateway edge on a Let's Encrypt certificate, driven by the console you already have.
It shares the storage account, Key Vault, ACR and identities of the home apply. The satellite's API server is
Entra-integrated, so the console's workload identity reaches it as a Kubernetes user named by its object id; the
module gives that identity *Azure Kubernetes Service Cluster User Role* and *Reader* on the cluster, and the chart
binds it to the console's two ClusterRoles.

The console talks to workers by pod IP (per-pod smoke, metrics, reload, canary), not through the edge, so the
satellite's network is joined to the home one. The home cluster uses Azure CNI overlay on an AKS-managed VNet
(`aks-vnet-<n>` in its node resource group, `10.224.0.0/12`), whose pod addresses are cluster-local; the satellite
therefore gets its own VNet (`vnet_cidr`, `10.2.0.0/16` by default, which must not overlap the home one) and runs
Azure CNI without overlay, so its pods carry VNet addresses. The module peers the two VNets both ways and adds a
network security group on the satellite's nodes subnet that lets the home VNet's address space in on port 8080. The
home VNet comes from the home apply's outputs `vnet_id`, `vnet_name` and `vnet_resource_group` (an install applied
before 1.0.0b gets them on its next `terraform apply`).

```sh
SAT=westeurope; HOME_RG=$(terraform -chdir=deploy/terraform/azure output -raw resource_group)
az identity show -g $HOME_RG -n ramen-console --query principalId -o tsv     # console_principal_id
az acr show -n $(terraform -chdir=deploy/terraform/azure output -raw acr | cut -d. -f1) --query id -o tsv   # acr_id
for o in vnet_id vnet_name vnet_resource_group; do terraform -chdir=deploy/terraform/azure output -raw $o; echo; done  # home_vnet_id, home_vnet_name, home_vnet_resource_group
cd deploy/terraform/azure-satellite && cp terraform.tfvars.example terraform.tfvars   # subscription_id, location=$SAT, the ids and names above
terraform init && terraform apply                  # about fifteen minutes
$(terraform output -raw kubeconfig_command)        # kubectl now points at the satellite (admin credentials)
cd ../../..
kubectl create ns ramen-system
kubectl label ns ramen-system ramen.io/routes=true
helm upgrade --install ramen-satellite deploy/helm/ramen-satellite -n ramen-system \
  $(terraform -chdir=deploy/terraform/azure-satellite output -raw helm_set)
kubectl -n ramen-system get certificate edge      # READY True within a minute or two
terraform -chdir=deploy/terraform/azure-satellite output -raw public_hostname
```

`helm_set` expands to `--set provider=azure,region=$SAT --set console.identity=<object id> --set azure.nodeResourceGroup=MC_…,azure.publicIpName=ramen-edge --set azure.publicHostname=<ip-with-dashes>.sslip.io`.
`--set azure.tls=selfsigned` and `azure.acmeServer` work as for the console. By default the module creates its own
resource group `ramen-<suffix>-<location>`; `resource_group = "<name>"` puts the cluster in an existing one instead.
Keep one Terraform state per satellite.

Then in the console, as a super admin: **Config → Clusters → Add satellite** (the provider is the console's cloud,
`azure`): region `$SAT`, cluster name (`ramen` by default), the `public_hostname` output as host, and the
satellite's resource group in **Resource group (if not the console's)**. From now on the location picker offers `$SAT-<n>` zones too, a zone placed there gets its namespace,
identity (with a federated credential for the satellite's OIDC issuer) and route on the satellite, and its clients
call `https://<host>/mcp` with the same headers and keys; the zone pages and the sign-in guide name the right host
per zone.

Caveats:

- **Telemetry.** The Config page's one OTLP endpoint must be reachable from every cluster. The chart's try-out
  collectors (Jaeger, Phoenix) live in the home cluster and are cluster-local, so a satellite's workers log
  `telemetry export failed` until you point the endpoint at a collector they can reach.
- **Cost.** The second control plane is free (free tier); its nodes (two `Standard_B2s_v2` by default), a second
  Standard public IP and load balancer rule, at the rates above, and inter-region VNet peering traffic for the
  console's calls (cents). Zones and sizes depend on the subscription in the
  new location as well (Prerequisites).
- **Clean-up order.** Delete the satellite's zones in the console first (that frees their namespaces, identities
  and routes and lets the Clusters card drop the satellite), then `helm uninstall ramen-satellite -n ramen-system`
  on the satellite, then `terraform -chdir=deploy/terraform/azure-satellite destroy` (removes the resource group it
  created, never one it reused).

## Teardown

```sh
# in the console: delete the group, which removes its namespaces, identities, role assignments and routes
helm uninstall ramen -n ramen-system
terraform -chdir=deploy/terraform/azure destroy
```

`terraform destroy` removes the resource group, the cluster and its node resource group (with the public IP), and
purges the Key Vault (soft delete would otherwise keep its name for a week). The console's own secrets
(`ramenc--…`, tag `ramen=console`: identity-provider and SMTP secrets, LLM provider keys) and every group secret live
in that vault, so they go with it; nothing outside the resource group needs deleting. Check nothing is left with
`az group list --query "[?starts_with(name,'ramen')]" -o table`.

## Gotchas

- `MissingSubscriptionRegistration` from Terraform: register the resource providers listed above.
- The certificate stays `False` while Let's Encrypt cannot reach port 80 of the public IP: check
  `kubectl -n ramen-system describe challenge`, or use `azure.tls=selfsigned`.
- A zone needs a node in its availability zone. A Ramen zone `eastus-2` with the node pool only in zone 1 leaves its
  workers `Pending`; add `"2"` to `node_zones` and apply. On a cluster without zones the console sends
  `nodeSelector: null`, which also clears a zone pin an earlier deploy left.
- `AvailabilityZoneNotSupported` or a refused VM size: see Prerequisites (`node_zones = []`, `Standard_B2s_v2`).
- The default node size needs quota in its own VM family (a subscription can have regional cores free and none in
  that family), and some sizes are not offered for AKS in every region. The error message lists the sizes that
  are; set `node_size` to one of them.
- A client that omits the two headers gets the console, not a worker.
