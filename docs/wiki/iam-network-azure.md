# IAM and network on Azure

Every identity an AKS install of Ramen uses, what each may do, and every network path, read from
`deploy/terraform/azure/`, `deploy/terraform/azure-satellite/`, the chart `deploy/helm/ramen` (`provider=azure`) and the
console's Azure adapter (`console/src/ramen_console/cloud/azure*.py`). Terraform creates nothing per group, and every
role it assigns is scoped to the install's resource group or one resource in it — nothing on the subscription. The
install itself: [Deploy on Azure](deploy-azure.md).

## Identities at a glance

| Identity | Kind | Created by | Used by |
|---|---|---|---|
| `ramen-console` | user-assigned managed identity, federated with `system:serviceaccount:ramen-system:console` | Terraform | the console pod |
| AKS cluster identity (system-assigned) and kubelet identity | managed identities | AKS | the control plane, the nodes |
| `ramen-<group>-<zone>` | user-assigned managed identity, federated with `ramen-<group>-<zone>/worker` | the console, when a zone is bound | that zone's worker pods |
| `ramen-build-<group>` | user-assigned managed identity (tag `ramen-build-group`), federated with `ramen-system/ramen-build-<group>` | the console, on the group's first build | the group's image-build Job |
| `ramen-evals` | user-assigned managed identity, federated with `ramen-system/ramen-evals` | Terraform | the evals and SkillSpector scan Jobs |

Federated credentials trust the AKS OIDC issuer with audience `api://AzureADTokenExchange` and the exact subject.

## The console

Role assignments on the resource group:

| Role | Why |
|---|---|
| Storage Blob Data Contributor | sync group repos into the `groups` container |
| Storage Table Data Contributor | the console's state (table `ramen`) |
| Key Vault Secrets Officer | group secrets `ramen--<group>--<env>--<zone>--<VAR>` (`all` when unscoped) and the console's own secrets (`ramenc--…`) |
| Managed Identity Contributor | zone and build identities and their federated credentials |
| Role Based Access Control Administrator | assign the zone and build identities their roles; used only for the permission catalogue's roles and the registry roles below |

On the registry: a custom data-plane role "Ramen group repository cleaner" (`repositories/metadata/read`,
`metadata/delete`, `content/delete` — no write) under the ABAC condition that the repository name starts with
`ramen/groups/`: it removes a deleted group's images and can push nothing. Azure OpenAI and AI Foundry are key-based,
so the judge proxy needs no role; the key stays in the console.

Inside the cluster: the chart's ClusterRole `ramen-console`, the per-zone `ramen-console-zone` RoleBinding and the
`ramen-console-build` Role ([IAM and network on GCP](iam-network-gcp.md#the-console)), plus Envoy Gateway
`SecurityPolicy` and `BackendTrafficPolicy` (edge IP rules and route timeouts), Gateway API `BackendTLSPolicy` (each
zone's backend TLS, D86) and the `ramen-console-tls` Role in `ramen-system` (the chart services' leaves and `ramen-ca`).

**Residual.** Role Based Access Control Administrator lets a compromised console assign itself any role inside the
resource group, the registry's writer roles included. Platform images can be pinned by digest (`image.digest`,
`image.workerDigest`) so a re-tag does not reach a pod until an operator changes it; the console is the platform's
crown jewel on every cloud ([Threat model](../threat-model.md)).

## Worker identities

`ramen-<group>-<zone>` gets, from the console:

- **Storage Blob Data Reader** on the `groups` container with an ABAC condition (`conditionVersion 2.0`): blob reads
  only where `@Resource[…/containers/blobs:path] StringStartsWith '<group>/'`, listing only where
  `@Request[…/containers/blobs:prefix] StringStartsWith '<group>/'`. The storage account is general-purpose v2, which
  conditions need.
- **Key Vault Secrets User** on each of the group's secrets, one assignment per secret.

Approved permission requests add roles in the install's resource group (Log Analytics Contributor, Monitoring
Metrics Publisher, Service Bus Data Sender/Receiver, Storage Table Data Contributor, Key Vault Crypto User, Cognitive
Services User — [service-account permissions](users-access.md#service-account-permissions)).

## Image builds, evals and scans

- **Builds (D85).** The registry runs in `AbacRepositoryPermissions` mode, where ACR honours the
  "Container Registry Repository *" roles and their conditions (not `AcrPull` / `AcrPush`). `ramen-build-<group>` gets
  **Container Registry Repository Writer** conditioned on the repository `ramen/groups/<group>/worker` and
  **Container Registry Repository Reader** on the worker base repository. No build identity can write
  `ramen/console`, `ramen/worker` or `ramen/skillspector`.
- **Evals and scans.** `ramen-evals` has no role assignment at all. Pod logs reach Azure Monitor through the node
  agent, not this identity.
- **Nodes.** The kubelet identity holds Container Registry Repository Reader on the registry (every repository).

## Network

| Path | From → to | Port | Control |
|---|---|---|---|
| Clients → edge | internet → static public IP `ramen-edge` (node resource group) → Envoy Gateway in `envoy-gateway-system` | 443; 80 only for the Let's Encrypt HTTP-01 challenge and a redirect to https | certificate from Let's Encrypt via cert-manager (or `azure.tls=selfsigned`); the Envoy Service keeps the client address (`externalTrafficPolicy: Local`) |
| Edge → console, workers | Envoy pods → console 8000, worker pods 8080 | 8000, 8080 | TLS (D86): `BackendTLSPolicy` `console` (hostname `console.ramen-system.svc.cluster.local`) and one per zone on Service `worker` (hostname `<group>.<zone>.worker.ramen.internal`, D92), both verifying against ConfigMap `ramen-ca`; HTTPRoutes matching `ramen-group` + `ramen-zone`; an Envoy `SecurityPolicy` per zone for IP rules; NetworkPolicy `worker` admits the `envoy-gateway-system` namespace |
| Console → workers | console pod → worker pod IPs | 8080 | gRPC over TLS verified as the zone's `<group>.<zone>.worker.ramen.internal` against the install CA; admin key; NetworkPolicy `worker` |
| Workers → console, Redis, collector, internet | as on GCP | 8000, 6379/10000, 4318, any | TLS to each; `worker-egress` (written only with `cluster.serviceCidr`, which `helm_set` does not carry; AKS default `10.0.0.0/16`) |
| Workers, console → Azure Managed Redis | → the instance's public endpoint | 10000 (TLS) | access key in Secret `ramen-redis` (`rediss://`); `managed_redis = true` by default |
| Home → satellite | home VNet → satellite nodes and pods | 8080 | VNet peering both ways; NSG rule `ramen-home-to-workers`; the satellite zone's NetworkPolicy `worker` admits `console.sourceCidrs` (the home node subnet the overlay SNATs the console's calls to — never a pod range: Calico matches pod addresses by `ipBlock`), a home zone never does |

- **Cluster network.** Azure CNI in overlay mode, standard load balancer, `network_policy = "calico"` (since
  1.0.0c; the satellite module too, on its node-subnet Azure CNI). AKS enforces Kubernetes NetworkPolicies only with
  a policy engine chosen on the cluster; Calico is the one both CNI modes take (azurerm accepts Cilium only with
  overlay or a pod subnet). Not yet checked live on a cloud run
  ([Threat model](../threat-model.md#findings-and-their-status), S6b). On an existing cluster the change may force
  Terraform to replace it — [Deploy on Azure → Day 2](deploy-azure.md#day-2).
- **IP rules** are enforced by the node (`RAMEN_ALLOWED_CIDRS`, one trusted forwarded hop) and mirrored at the edge
  as the zone's Envoy `SecurityPolicy` (deny unless the client CIDR is listed).
- **Managed Redis** is reached over its public endpoint; the access key is the gate. It replaces the in-cluster Redis
  for throttling, the shared task store and consent.
- **Storage and Key Vault** keep their public endpoints; access is Entra-only for workloads (workload identity); the
  storage account keeps shared keys on for Terraform's own data-plane calls.

## Satellites

`deploy/terraform/azure-satellite` adds an AKS cluster in another location with its own VNet (`vnet_cidr`, default
`10.2.0.0/16`, Azure CNI without overlay so pod IPs route over the peering; it must not overlap the home VNet,
`10.224.0.0/12` by default, or its service range), VNet peering both ways, an NSG on the node subnet allowing 8080 from
the home VNet's address space and 80/443 from the internet (the edge), Network Contributor on the VNet for the
cluster's identity, Container Registry Repository Reader on the home registry for its kubelet, and for the console's
identity **Azure Kubernetes Service Cluster User Role** and **Reader** on the satellite cluster. The API server uses
Entra ID with Kubernetes RBAC; the satellite chart binds the console's ClusterRoles to the console identity's object
id. [Deploy on Azure → Satellite clusters](deploy-azure.md#satellite-clusters-other-regions).

## Terraform switches

| Variable | Default | Effect |
|---|---|---|
| `managed_redis`, `managed_redis_sku`, `managed_redis_ha` | `true`, `Balanced_B0`, `false` | Azure Managed Redis for throttling and tasks; its URL (with the key) goes into `helm_set`, which is therefore sensitive |
| `node_zones` | `["1"]` | `[]` for subscriptions without availability zones |
| `otel_collector`, `evals_engine`, `skillspector_engine` | `none` | the optional engines; carried by `helm_set` |
