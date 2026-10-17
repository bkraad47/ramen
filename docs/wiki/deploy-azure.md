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
CNI overlay, one node pool in the zones of `node_zones`, or none), ACR, the storage account with the `groups` container and the `ramen` table,
Key Vault in RBAC mode, the edge's static public IP, the console's user-assigned identity with its federated
credential for `ramen-system/console`, and installs Envoy Gateway and cert-manager into the cluster. About fifteen
minutes. `terraform output helm_set` prints every Helm value the next steps need.

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

## 4. The first zone, group and deploy

Sign in at `https://<public_hostname>/` as `admin@ramen.local`, then the same steps as on GCP: a zone with
provider `azure` and region `eastus-1` (Ramen names Azure zones `<location>-<n>`; with `node_zones` set, the node
pool must have a node in that zone, zone 1 by default; with `node_zones = []` any name of the location works, the
0.7.5 run used `westus2-1`), the `demo` group from the demo repo, an environment on that zone, an MCP key, a
canary deploy. Creating the environment makes the namespace `ramen-demo-<zone>`, a user-assigned identity
`ramen-demo-<zone>` with a federated credential for the namespace's `worker` service account, its role assignments
(blob reader on the `groups` container, Key Vault Secrets User on exactly the group's secrets), and an HTTPRoute on
the Gateway matching the two headers.

```sh
curl https://$HOSTNAME/mcp -H "Authorization: Bearer $RAMEN_MCP_KEY" -H 'Content-Type: application/json' \
  -H 'ramen-group: demo' -H 'ramen-zone: eastus-1' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
ramen-mcp-bridge --target $HOSTNAME:443 --tls --key "$RAMEN_MCP_KEY" --group demo --zone eastus-1
```

`scripts/cloud_smoke.sh` does the whole step: `RAMEN_NODE_TLS=1 RAMEN_ZONE_PROVIDER=azure scripts/cloud_smoke.sh
https://$HOSTNAME admin@ramen.local <password> <admin key> $HOSTNAME:443`.

Sign-in through Microsoft Entra ID or Google works exactly as on the other clouds ([Entra ID and
Workspace](sso.md)); register `https://<public_hostname>/auth/<provider>/callback` on the app.

## The console's identity and access controls

| It may | How |
|---|---|
| Sync group repos into the `groups` container | Storage Blob Data Contributor on the resource group |
| Keep its own state | Storage Table Data Contributor (table `ramen`) |
| Manage group secrets | Key Vault Secrets Officer; secrets are named `ramen--<group>--<VAR>` |
| Create and delete zone identities | Managed Identity Contributor (identities and their federated credentials) |
| Grant a zone identity its roles | Role Based Access Control Administrator, used only for the permission catalogue's roles |
| Manage the zone namespaces and routes | The chart's ClusterRole, the same as on GCP, plus Envoy Gateway `SecurityPolicy` / `BackendTrafficPolicy` |

Every role is scoped to the install's resource group; nothing is granted on the subscription. Azure cannot scope a
blob role to a path, so a zone identity may read the whole `groups` container: the per-group boundary for code is the
console's sync (each worker loads only its group's prefix) and for secrets the per-secret role assignment.

## Day 2

- **IP rules**: a CIDR list per zone on the group page. The node enforces it (authoritative, as everywhere) and the
  console mirrors it at the edge as an Envoy Gateway `SecurityPolicy` on the zone's route. The Envoy Service keeps
  the client's address (`externalTrafficPolicy: Local`), so workers trust one forwarded hop.
- **Logs**: read from the pods through the Kubernetes API; no Log Analytics workspace is needed.
- **Upgrade the console**: push the images, then `kubectl -n ramen-system rollout restart deploy/console`. The
  rollout keeps one ready pod and drains the old one for 40 s, so the edge never answers without a backend.
- **Scaling**: set a max per zone on the group page and turn on the scheduler on the Config page; Ramen scales the
  workers between 1 and the max, and the AKS cluster autoscaler (`node_max`) adds nodes for them
  ([Autoscaling](autoscaling.md)).

## Teardown

```sh
# in the console: delete the group, which removes its namespaces, identities, role assignments and routes
helm uninstall ramen -n ramen-system
terraform -chdir=deploy/terraform/azure destroy
```

`terraform destroy` removes the resource group, the cluster and its node resource group (with the public IP), and
purges the Key Vault (soft delete would otherwise keep its name for a week). Check nothing is left with
`az group list --query "[?starts_with(name,'ramen')]" -o table`.

## Gotchas

- `MissingSubscriptionRegistration` from Terraform: register the resource providers listed above.
- The certificate stays `False` while Let's Encrypt cannot reach port 80 of the public IP: check
  `kubectl -n ramen-system describe challenge`, or use `azure.tls=selfsigned`.
- A zone needs a node in its availability zone. A Ramen zone `eastus-2` with the node pool only in zone 1 leaves its
  workers `Pending`; add `"2"` to `node_zones` and apply. On a cluster without zones the console sends
  `nodeSelector: null`, which also clears a zone pin an earlier deploy left.
- `AvailabilityZoneNotSupported` or a refused VM size: see Prerequisites (`node_zones = []`, `Standard_B2s_v2`).
- A client that omits the two headers gets the console, not a worker.
